from __future__ import annotations

import json
import sqlite3
import time
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import BACKUP_DIR, DATA_DIR, DB_PATH, DEFAULT_PAGE_SIZE
from .metadata import (
    UNKNOWN_AUTHOR,
    UNKNOWN_TITLE,
    build_simple_description,
    clean_description,
    is_suspicious_author,
    is_suspicious_title,
)
from .tagger import refine_tags, merge_tags


SQLITE_PARAM_LIMIT = 900
REMOTE_LOOKUP_VERSION = 2

SORT_MAP = {
    "title": "lower(b.title)",
    "author": "lower(b.authors)",
    "updated": "b.updated_at",
    "added": "b.added_at",
    "size": "b.size",
    "path": "lower(b.path)",
    "format": "lower(b.ext)",
    "douban": "CAST(NULLIF(b.douban_rating, '') AS REAL)",
}


BOOK_COLUMN_DEFAULTS = {
    "publisher": "TEXT NOT NULL DEFAULT ''",
    "remote_source": "TEXT NOT NULL DEFAULT ''",
    "remote_url": "TEXT NOT NULL DEFAULT ''",
    "douban_rating": "TEXT NOT NULL DEFAULT ''",
    "douban_url": "TEXT NOT NULL DEFAULT ''",
    "douban_rating_status": "TEXT NOT NULL DEFAULT ''",
    "cover_path": "TEXT NOT NULL DEFAULT ''",
    "quarantine_path": "TEXT NOT NULL DEFAULT ''",
    "scan_error": "TEXT NOT NULL DEFAULT ''",
    "last_scanned_at": "REAL NOT NULL DEFAULT 0",
    "last_remote_lookup_at": "REAL NOT NULL DEFAULT 0",
    "remote_lookup_version": "INTEGER NOT NULL DEFAULT 0",
}


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                ext TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                mtime REAL NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                authors TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                publisher TEXT NOT NULL DEFAULT '',
                published_year TEXT NOT NULL DEFAULT '',
                isbn TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'local',
                remote_source TEXT NOT NULL DEFAULT '',
                remote_url TEXT NOT NULL DEFAULT '',
                douban_rating TEXT NOT NULL DEFAULT '',
                douban_url TEXT NOT NULL DEFAULT '',
                douban_rating_status TEXT NOT NULL DEFAULT '',
                cover_path TEXT NOT NULL DEFAULT '',
                tags_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                quarantine_path TEXT NOT NULL DEFAULT '',
                scan_error TEXT NOT NULL DEFAULT '',
                added_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_scanned_at REAL NOT NULL DEFAULT 0,
                last_remote_lookup_at REAL NOT NULL DEFAULT 0,
                remote_lookup_version INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS book_tags (
                book_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (book_id, tag),
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS operation_batches (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                confirm_phrase TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                total_size INTEGER NOT NULL,
                created_at REAL NOT NULL,
                started_at REAL NOT NULL DEFAULT 0,
                completed_at REAL NOT NULL DEFAULT 0,
                output_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS operation_items (
                operation_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                quarantine_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (operation_id, book_id),
                UNIQUE (operation_id, position),
                FOREIGN KEY (operation_id) REFERENCES operation_batches(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS background_jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                processed INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                state_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_books_status ON books(status);
            CREATE INDEX IF NOT EXISTS idx_books_ext ON books(ext);
            CREATE INDEX IF NOT EXISTS idx_books_mtime ON books(mtime);
            CREATE INDEX IF NOT EXISTS idx_books_status_updated ON books(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_books_status_ext ON books(status, ext);
            CREATE INDEX IF NOT EXISTS idx_book_tags_tag ON book_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_operation_batches_status ON operation_batches(status);
            CREATE INDEX IF NOT EXISTS idx_background_jobs_created ON background_jobs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_background_jobs_status ON background_jobs(status);
            """
        )
        _ensure_book_columns(conn)


def backup_database_once_per_day() -> Path | None:
    if not DB_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"bookvault_{time.strftime('%Y%m%d')}.sqlite3"
    if backup_path.exists():
        return backup_path
    with connect() as source:
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
    return backup_path


def reuse_remote_metadata_for_identical_copies() -> int:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                filename, size, mtime, title, authors,
                douban_rating, douban_url, douban_rating_status,
                remote_source, remote_url,
                last_remote_lookup_at, remote_lookup_version
            FROM books
            WHERE status = 'active'
            ORDER BY id
            """
        ).fetchall()
        sources: dict[tuple[str, int, int, str, str], sqlite3.Row | None] = {}
        targets: dict[tuple[str, int, int, str, str], list[int]] = {}
        for row in rows:
            signature = (
                str(row["filename"]),
                int(row["size"]),
                round(float(row["mtime"]) * 1000),
                str(row["title"]),
                str(row["authors"]),
            )
            if not str(row["douban_rating"] or ""):
                targets.setdefault(signature, []).append(int(row["id"]))
                continue
            existing = sources.get(signature)
            if existing is None and signature not in sources:
                sources[signature] = row
            elif existing is not None and (
                existing["douban_rating"] != row["douban_rating"]
                or existing["douban_url"] != row["douban_url"]
            ):
                sources[signature] = None

        candidates: list[tuple[sqlite3.Row, int]] = []
        for signature, source in sources.items():
            if source is None:
                continue
            candidates.extend((source, target_id) for target_id in targets.get(signature, []))
        if not candidates:
            return 0
        now = time.time()
        conn.executemany(
            """
            UPDATE books
            SET douban_rating = ?,
                douban_url = ?,
                douban_rating_status = ?,
                remote_source = ?,
                remote_url = ?,
                last_remote_lookup_at = ?,
                remote_lookup_version = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'active'
              AND COALESCE(douban_rating, '') = ''
            """,
            [
                (
                    source["douban_rating"],
                    source["douban_url"],
                    source["douban_rating_status"] or "已获取",
                    source["remote_source"],
                    source["remote_url"],
                    source["last_remote_lookup_at"],
                    source["remote_lookup_version"],
                    now,
                    target_id,
                )
                for source, target_id in candidates
            ],
        )
    return len(candidates)


def _ensure_book_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(books)").fetchall()}
    for name, ddl in BOOK_COLUMN_DEFAULTS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE books ADD COLUMN {name} {ddl}")


def save_background_job(job: dict[str, Any]) -> None:
    state_json = json.dumps(job, ensure_ascii=False, separators=(",", ":"))
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO background_jobs (
                id, kind, status, message, processed, total,
                created_at, updated_at, state_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind = excluded.kind,
                status = excluded.status,
                message = excluded.message,
                processed = excluded.processed,
                total = excluded.total,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                state_json = excluded.state_json
            """,
            (
                str(job["id"]),
                str(job.get("kind") or ""),
                str(job.get("status") or ""),
                str(job.get("message") or ""),
                int(job.get("processed") or 0),
                int(job.get("total") or 0),
                float(job.get("created_at") or time.time()),
                float(job.get("updated_at") or time.time()),
                state_json,
            ),
        )


def load_background_jobs(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM background_jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return _background_job_rows_to_dicts(rows)


def load_active_background_jobs() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM background_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY created_at DESC
            """
        ).fetchall()
    return _background_job_rows_to_dicts(rows)


def _background_job_rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for row in rows:
        try:
            job = json.loads(str(row["state_json"]))
        except (TypeError, ValueError):
            job = {}
        if not isinstance(job, dict):
            job = {}
        job.update(
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "status": str(row["status"]),
                "message": str(row["message"]),
                "processed": int(row["processed"]),
                "total": int(row["total"]),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
            }
        )
        job["payload"] = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        job["errors"] = job.get("errors") if isinstance(job.get("errors"), list) else []
        jobs.append(job)
    return jobs


def upsert_book(path: str | Path, stat_result: Any, metadata: dict[str, Any]) -> int:
    now = time.time()
    book_path = Path(path)
    tags = merge_tags(metadata.get("tags"))
    with connect() as conn:
        existing = conn.execute("SELECT * FROM books WHERE path = ?", (str(book_path),)).fetchone()
        if existing:
            book_id = int(existing["id"])
            reset_remote = bool(metadata.get("reset_remote"))
            merged_tags = merge_tags(get_tags_for_book(conn, book_id), tags)
            values = {
                "filename": book_path.name,
                "ext": book_path.suffix.lower(),
                "size": int(stat_result.st_size),
                "mtime": float(stat_result.st_mtime),
                "title": _prefer_title(metadata.get("title"), existing["title"]),
                "authors": _prefer_author(metadata.get("authors"), existing["authors"]),
                "description": _prefer(metadata.get("description"), existing["description"]),
                "language": _prefer(metadata.get("language"), existing["language"]),
                "publisher": _prefer(metadata.get("publisher"), existing["publisher"]),
                "published_year": _prefer(metadata.get("published_year"), existing["published_year"]),
                "isbn": _prefer(metadata.get("isbn"), existing["isbn"]),
                "source": _prefer(metadata.get("source"), existing["source"]),
                "remote_source": "" if reset_remote else _prefer(metadata.get("remote_source"), existing["remote_source"]),
                "remote_url": "" if reset_remote else _prefer(metadata.get("remote_url"), existing["remote_url"]),
                "douban_rating": "" if reset_remote else _prefer(metadata.get("douban_rating"), existing["douban_rating"]),
                "douban_url": "" if reset_remote else _prefer(metadata.get("douban_url"), existing["douban_url"]),
                "douban_rating_status": str(metadata.get("douban_rating_status") or "待查询") if reset_remote else _douban_status(metadata, existing),
                "cover_path": _prefer(metadata.get("cover_path"), existing["cover_path"]),
                "tags_text": ", ".join(merged_tags),
                "scan_error": metadata.get("scan_error", "") or "",
                "updated_at": now,
                "last_scanned_at": now,
                "remote_lookup_version": 0 if reset_remote else int(existing["remote_lookup_version"] or 0),
                "id": book_id,
            }
            merged_tags = refine_tags(values["title"], values["authors"], values["description"], merged_tags)
            values["tags_text"] = ", ".join(merged_tags)
            conn.execute(
                """
                UPDATE books
                SET filename = :filename,
                    ext = :ext,
                    size = :size,
                    mtime = :mtime,
                    title = :title,
                    authors = :authors,
                    description = :description,
                    language = :language,
                    publisher = :publisher,
                    published_year = :published_year,
                    isbn = :isbn,
                    source = :source,
                    remote_source = :remote_source,
                    remote_url = :remote_url,
                    douban_rating = :douban_rating,
                    douban_url = :douban_url,
                    douban_rating_status = :douban_rating_status,
                    cover_path = :cover_path,
                    tags_text = :tags_text,
                    status = 'active',
                    quarantine_path = '',
                    scan_error = :scan_error,
                    updated_at = :updated_at,
                    last_scanned_at = :last_scanned_at,
                    remote_lookup_version = :remote_lookup_version
                WHERE id = :id
                """,
                values,
            )
            replace_tags(conn, book_id, merged_tags)
            return book_id

        title = str(metadata.get("title") or book_path.stem)
        authors = str(metadata.get("authors") or "")
        description = str(metadata.get("description") or "")
        tags = refine_tags(title, authors, description, tags)
        cursor = conn.execute(
            """
            INSERT INTO books (
                path, filename, ext, size, mtime, title, authors, description,
                language, publisher, published_year, isbn, source,
                douban_rating, douban_url, douban_rating_status, cover_path, tags_text,
                status, scan_error, added_at, updated_at, last_scanned_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                str(book_path),
                book_path.name,
                book_path.suffix.lower(),
                int(stat_result.st_size),
                float(stat_result.st_mtime),
                title,
                authors,
                description,
                str(metadata.get("language") or ""),
                str(metadata.get("publisher") or ""),
                str(metadata.get("published_year") or ""),
                str(metadata.get("isbn") or ""),
                str(metadata.get("source") or "local"),
                str(metadata.get("douban_rating") or ""),
                str(metadata.get("douban_url") or ""),
                str(metadata.get("douban_rating_status") or ""),
                str(metadata.get("cover_path") or ""),
                ", ".join(tags),
                str(metadata.get("scan_error") or ""),
                now,
                now,
                now,
            ),
        )
        book_id = int(cursor.lastrowid)
        replace_tags(conn, book_id, tags)
        return book_id


def get_book_scan_snapshot(path: str | Path) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM books WHERE path = ?", (str(Path(path)),)).fetchone()
    return _row_to_dict(row) if row else None


def get_scan_snapshots() -> dict[str, dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, path, filename, size, mtime, title, authors, description, tags_text,
                   douban_rating, douban_rating_status, last_remote_lookup_at, status
                   , remote_lookup_version
            FROM books
            """
        ).fetchall()
    return {str(row["path"]): _row_to_dict(row) for row in rows}


def get_book_remote_snapshot(book_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (int(book_id),)).fetchone()
    return _row_to_dict(row) if row else None


def touch_book_scan(book_id: int, path: str | Path, stat_result: Any) -> None:
    now = time.time()
    book_path = Path(path)
    with connect() as conn:
        conn.execute(
            """
            UPDATE books
            SET filename = ?,
                ext = ?,
                size = ?,
                mtime = ?,
                status = 'active',
                quarantine_path = '',
                updated_at = CASE WHEN status != 'active' THEN ? ELSE updated_at END,
                last_scanned_at = ?
            WHERE id = ?
            """,
            (
                book_path.name,
                book_path.suffix.lower(),
                int(stat_result.st_size),
                float(stat_result.st_mtime),
                now,
                now,
                int(book_id),
            ),
        )


def touch_books_scan_batch(items: Iterable[tuple[int, str | Path, Any]]) -> int:
    now = time.time()
    records = []
    for book_id, path, stat_result in items:
        book_path = Path(path)
        records.append(
            (
                book_path.name,
                book_path.suffix.lower(),
                int(stat_result.st_size),
                float(stat_result.st_mtime),
                now,
                now,
                int(book_id),
            )
        )
    if not records:
        return 0
    with connect() as conn:
        conn.executemany(
            """
            UPDATE books
            SET filename = ?,
                ext = ?,
                size = ?,
                mtime = ?,
                status = 'active',
                quarantine_path = '',
                updated_at = CASE WHEN status != 'active' THEN ? ELSE updated_at END,
                last_scanned_at = ?
            WHERE id = ?
            """,
            records,
        )
    return len(records)


def update_book_remote(book_id: int, metadata: dict[str, Any]) -> None:
    now = time.time()
    with connect() as conn:
        current = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not current:
            return
        current_tags = get_tags_for_book(conn, book_id)
        new_tags = merge_tags(metadata.get("tags")) if metadata.get("tags") else []
        merged_tags = merge_tags(current_tags, new_tags)
        values = {
            "title": _prefer(metadata.get("title"), current["title"]),
            "authors": _prefer(metadata.get("authors"), current["authors"]),
            "description": _prefer(metadata.get("description"), current["description"]),
            "language": _prefer(metadata.get("language"), current["language"]),
            "publisher": _prefer(metadata.get("publisher"), current["publisher"]),
            "published_year": _prefer(metadata.get("published_year"), current["published_year"]),
            "isbn": _prefer(metadata.get("isbn"), current["isbn"]),
            "remote_source": str(metadata.get("remote_source") or current["remote_source"] or ""),
            "remote_url": str(metadata.get("remote_url") or current["remote_url"] or ""),
            "douban_rating": _prefer(metadata.get("douban_rating"), current["douban_rating"]),
            "douban_url": _prefer(metadata.get("douban_url"), current["douban_url"]),
            "douban_rating_status": _douban_status(metadata, current),
            "cover_path": _prefer(metadata.get("cover_path"), current["cover_path"]),
            "tags_text": ", ".join(merged_tags),
            "updated_at": now,
            "last_remote_lookup_at": now,
            "remote_lookup_version": REMOTE_LOOKUP_VERSION,
            "id": book_id,
        }
        merged_tags = refine_tags(values["title"], values["authors"], values["description"], merged_tags)
        values["tags_text"] = ", ".join(merged_tags)
        conn.execute(
            """
            UPDATE books
            SET title = :title,
                authors = :authors,
                description = :description,
                language = :language,
                publisher = :publisher,
                published_year = :published_year,
                isbn = :isbn,
                remote_source = :remote_source,
                remote_url = :remote_url,
                douban_rating = :douban_rating,
                douban_url = :douban_url,
                douban_rating_status = :douban_rating_status,
                cover_path = :cover_path,
                tags_text = :tags_text,
                updated_at = :updated_at,
                last_remote_lookup_at = :last_remote_lookup_at,
                remote_lookup_version = :remote_lookup_version
            WHERE id = :id
            """,
            values,
        )
        replace_tags(conn, book_id, merged_tags)


def add_manual_tags(book_ids: Iterable[int], tags: Iterable[str]) -> int:
    normalized = merge_tags(tags)
    if not normalized:
        return 0
    ids = [int(item) for item in book_ids if str(item).isdigit()]
    if not ids:
        return 0
    now = time.time()
    with connect() as conn:
        current_tags = get_tags_for_books(conn, ids)
        tag_rows = [(book_id, tag) for book_id in ids for tag in normalized]
        conn.executemany("INSERT OR IGNORE INTO book_tags(book_id, tag) VALUES (?, ?)", tag_rows)
        updates = [
            (", ".join(merge_tags(current_tags.get(book_id, []), normalized)), now, book_id)
            for book_id in ids
        ]
        conn.executemany("UPDATE books SET tags_text = ?, updated_at = ? WHERE id = ?", updates)
    return len(ids)


def set_quarantined(book_id: int, quarantine_path: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE books
            SET status = 'quarantined',
                quarantine_path = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (quarantine_path, time.time(), book_id),
        )


def create_operation_batch(
    operation_id: str,
    *,
    kind: str,
    confirm_phrase: str,
    items: list[dict[str, Any]],
) -> None:
    now = time.time()
    total_size = sum(int(item["size"]) for item in items)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO operation_batches (
                id, kind, status, confirm_phrase, item_count, total_size, created_at
            )
            VALUES (?, ?, 'pending', ?, ?, ?, ?)
            """,
            (operation_id, kind, confirm_phrase, len(items), total_size, now),
        )
        conn.executemany(
            """
            INSERT INTO operation_items (
                operation_id, position, book_id, path, size, mtime, title
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    operation_id,
                    position,
                    int(item["id"]),
                    str(item["path"]),
                    int(item["size"]),
                    float(item["mtime"]),
                    str(item.get("title") or item.get("filename") or ""),
                )
                for position, item in enumerate(items)
            ],
        )


def get_operation_batch(operation_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM operation_batches WHERE id = ?",
            (operation_id,),
        ).fetchone()
        if not batch:
            return None
        rows = conn.execute(
            """
            SELECT *
            FROM operation_items
            WHERE operation_id = ?
            ORDER BY position
            """,
            (operation_id,),
        ).fetchall()
    result = _row_to_dict(batch)
    result["items"] = [_row_to_dict(row) for row in rows]
    return result


def claim_operation_batch(operation_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE operation_batches
            SET status = 'executing',
                started_at = ?,
                error = ''
            WHERE id = ?
              AND status = 'pending'
            """,
            (time.time(), operation_id),
        )
        claimed = cursor.rowcount == 1
    return claimed


def update_operation_item(
    operation_id: str,
    book_id: int,
    *,
    status: str,
    quarantine_path: str = "",
    error: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE operation_items
            SET status = ?,
                quarantine_path = ?,
                error = ?
            WHERE operation_id = ?
              AND book_id = ?
            """,
            (status, quarantine_path, error[:500], operation_id, int(book_id)),
        )


def finish_operation_batch(
    operation_id: str,
    *,
    status: str,
    output_path: str = "",
    error: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE operation_batches
            SET status = ?,
                completed_at = ?,
                output_path = ?,
                error = ?
            WHERE id = ?
            """,
            (status, time.time(), output_path, error[:1000], operation_id),
        )


def set_quarantined_if_unchanged(item: dict[str, Any], quarantine_path: str) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE books
            SET status = 'quarantined',
                quarantine_path = ?,
                updated_at = ?
            WHERE id = ?
              AND path = ?
              AND size = ?
              AND ABS(mtime - ?) < 0.001
              AND status = 'active'
            """,
            (
                quarantine_path,
                time.time(),
                int(item["book_id"]),
                str(item["path"]),
                int(item["size"]),
                float(item["mtime"]),
            ),
        )
        changed = cursor.rowcount == 1
    return changed


def list_books(filters: dict[str, Any], page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = min(200, max(10, int(page_size or DEFAULT_PAGE_SIZE)))
    offset = (page - 1) * page_size
    sort = str(filters.get("sort") or "updated")
    order = "ASC" if str(filters.get("order") or "desc").lower() == "asc" else "DESC"
    sort_sql = SORT_MAP.get(sort, SORT_MAP["updated"])
    where_sql, params = _build_where(filters)
    rank_sql, rank_params = _search_rank_sql(str(filters.get("query") or ""))
    order_sql = (
        f"{rank_sql} DESC, length(b.title) ASC, {sort_sql} {order}, b.id DESC"
        if rank_sql
        else f"{sort_sql} {order}, b.id DESC"
    )

    with connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM books b WHERE {where_sql}", params).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT b.*
            FROM books b
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, *rank_params, page_size, offset),
        ).fetchall()
        tags_by_book = get_tags_for_books(conn, [int(row["id"]) for row in rows])
        items = [_book_to_dict(row, tags_by_book.get(int(row["id"]), [])) for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def select_book_ids(filters: dict[str, Any], exclude_ids: Iterable[int] | None = None, limit: int | None = None) -> list[int]:
    where_sql, params = _build_where(filters)
    exclude = [int(item) for item in (exclude_ids or []) if str(item).isdigit()]
    filter_exclude_in_python = len(exclude) > SQLITE_PARAM_LIMIT
    if exclude and not filter_exclude_in_python:
        placeholders = ",".join("?" for _ in exclude)
        where_sql += f" AND b.id NOT IN ({placeholders})"
        params.extend(exclude)
    limit_sql = ""
    if limit and not filter_exclude_in_python:
        limit_sql = " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(f"SELECT b.id FROM books b WHERE {where_sql} ORDER BY b.id{limit_sql}", params).fetchall()
    ids = [int(row["id"]) for row in rows]
    if filter_exclude_in_python:
        excluded = set(exclude)
        ids = [book_id for book_id in ids if book_id not in excluded]
        if limit:
            ids = ids[: int(limit)]
    return ids


def get_books_by_ids(book_ids: Iterable[int]) -> list[dict[str, Any]]:
    ids = [int(item) for item in book_ids if str(item).isdigit()]
    if not ids:
        return []
    with connect() as conn:
        rows: list[sqlite3.Row] = []
        for chunk in _chunks(ids, SQLITE_PARAM_LIMIT):
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(conn.execute(f"SELECT * FROM books WHERE id IN ({placeholders})", chunk).fetchall())
        tags_by_book = get_tags_for_books(conn, [int(row["id"]) for row in rows])
    by_id = {int(row["id"]): _book_to_dict(row, tags_by_book.get(int(row["id"]), [])) for row in rows}
    return [by_id[item] for item in ids if item in by_id]


def get_tags() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT bt.tag, COUNT(*) AS count
            FROM book_tags bt
            JOIN books b ON b.id = bt.book_id
            WHERE b.status = 'active'
            GROUP BY bt.tag
            ORDER BY count DESC, bt.tag ASC
            """
        ).fetchall()
    return [{"tag": row["tag"], "count": int(row["count"])} for row in rows]


def get_formats() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT ext, COUNT(*) AS count
            FROM books
            WHERE status = 'active'
            GROUP BY ext
            ORDER BY count DESC, ext ASC
            """
        ).fetchall()
    return [{"ext": row["ext"], "count": int(row["count"])} for row in rows]


def get_stats() -> dict[str, Any]:
    with connect() as conn:
        status_rows = conn.execute("SELECT status, COUNT(*) AS count FROM books GROUP BY status").fetchall()
        active_size = conn.execute("SELECT COALESCE(SUM(size), 0) FROM books WHERE status = 'active'").fetchone()[0]
        remote_stats = _remote_lookup_stats(conn)
    return {
        "db_path": str(DB_PATH),
        "statuses": {row["status"]: int(row["count"]) for row in status_rows},
        "active_size": int(active_size or 0),
        "remote_lookup": remote_stats,
    }


def get_remote_lookup_stats() -> dict[str, int]:
    with connect() as conn:
        return _remote_lookup_stats(conn)


def select_remote_lookup_due_ids(limit: int | None = None) -> list[int]:
    now = time.time()
    params: list[Any] = [REMOTE_LOOKUP_VERSION, now - 24 * 60 * 60]
    limit_sql = ""
    if limit:
        limit_sql = " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id
            FROM books
            WHERE status = 'active'
              AND COALESCE(douban_rating, '') = ''
              AND (
                    COALESCE(douban_rating_status, '') IN ('', '待查询', '缺少书名')
                    OR (douban_rating_status IN ('未找到', '无评分') AND COALESCE(remote_lookup_version, 0) < ?)
                    OR (douban_rating_status IN ('查询失败', '豆瓣访问受限') AND COALESCE(last_remote_lookup_at, 0) < ?)
                  )
            ORDER BY
                CASE
                    WHEN title GLOB '*[一-鿿]*' OR authors GLOB '*[一-鿿]*' THEN 0
                    ELSE 1
                END,
                last_remote_lookup_at ASC,
                id ASC
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [int(row["id"]) for row in rows]


def _remote_lookup_stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT
            SUM(CASE WHEN COALESCE(douban_rating, '') != '' THEN 1 ELSE 0 END) AS rated,
            SUM(CASE WHEN douban_rating_status = '待查询' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN douban_rating_status IN ('查询失败', '豆瓣访问受限') THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN douban_rating_status = '豆瓣访问受限' THEN 1 ELSE 0 END) AS access_limited,
            SUM(CASE WHEN douban_rating_status IN ('未找到', '无评分') THEN 1 ELSE 0 END) AS not_found_or_unrated,
            COUNT(*) AS total
        FROM books
        WHERE status = 'active'
        """
    ).fetchone()
    due = len(select_remote_lookup_due_ids()) if conn is not None else 0
    return {
        "rated": int(rows["rated"] or 0),
        "pending": int(rows["pending"] or 0),
        "failed": int(rows["failed"] or 0),
        "access_limited": int(rows["access_limited"] or 0),
        "not_found_or_unrated": int(rows["not_found_or_unrated"] or 0),
        "total": int(rows["total"] or 0),
        "due": due,
    }


def replace_tags(conn: sqlite3.Connection, book_id: int, tags: Iterable[str]) -> None:
    clean_tags = merge_tags(tags)
    conn.execute("DELETE FROM book_tags WHERE book_id = ?", (book_id,))
    conn.executemany("INSERT OR IGNORE INTO book_tags(book_id, tag) VALUES (?, ?)", [(book_id, tag) for tag in clean_tags])


def get_tags_for_book(conn: sqlite3.Connection, book_id: int) -> list[str]:
    rows = conn.execute("SELECT tag FROM book_tags WHERE book_id = ? ORDER BY tag", (book_id,)).fetchall()
    return [row["tag"] for row in rows]


def get_tags_for_books(conn: sqlite3.Connection, book_ids: list[int]) -> dict[int, list[str]]:
    if not book_ids:
        return {}
    rows: list[sqlite3.Row] = []
    for chunk in _chunks(book_ids, SQLITE_PARAM_LIMIT):
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            conn.execute(
                f"SELECT book_id, tag FROM book_tags WHERE book_id IN ({placeholders}) ORDER BY tag",
                chunk,
            ).fetchall()
        )
    tags: dict[int, list[str]] = {}
    for row in rows:
        book_id = int(row["book_id"])
        tag = str(row["tag"])
        bucket = tags.setdefault(book_id, [])
        if tag not in bucket:
            bucket.append(tag)
    return tags


def _build_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    status = str(filters.get("status") or "active")
    if status != "all":
        clauses.append("b.status = ?")
        params.append(status)

    query = str(filters.get("query") or "").strip()
    if query:
        term_groups = _search_term_groups(query)
        search_columns = ["b.title", "b.authors", "b.filename", "b.tags_text", "b.isbn", "b.publisher"]
        for terms in term_groups:
            column_clauses = [_like_any_sql(column, terms, params) for column in search_columns]
            clauses.append("(" + " OR ".join(column_clauses) + ")")

    ext = str(filters.get("format") or "").strip().lower()
    if ext:
        if not ext.startswith("."):
            ext = f".{ext}"
        clauses.append("b.ext = ?")
        params.append(ext)

    for idx, tag in enumerate(_coerce_tags(filters.get("tags"))):
        alias = f"bt{idx}"
        clauses.append(f"EXISTS (SELECT 1 FROM book_tags {alias} WHERE {alias}.book_id = b.id AND {alias}.tag = ?)")
        params.append(tag)

    return " AND ".join(clauses) if clauses else "1 = 1", params


def _search_rank_sql(query: str) -> tuple[str, list[Any]]:
    terms = _search_terms(query)
    if not terms:
        return "", []
    params: list[Any] = []
    title_exact = _exact_any_sql("b.title", terms, params)
    author_exact = _exact_any_sql("b.authors", terms, params)
    title_like = _like_any_sql("b.title", terms, params)
    author_like = _like_any_sql("b.authors", terms, params)
    filename_like = _like_any_sql("b.filename", terms, params)
    tag_like = _like_any_sql("b.tags_text", terms, params)
    isbn_like = _like_any_sql("b.isbn", terms, params)
    publisher_like = _like_any_sql("b.publisher", terms, params)
    return (
        f"""
        CASE
            WHEN {title_exact} THEN 130
            WHEN {author_exact} THEN 120
            WHEN {title_like} THEN 100
            WHEN {author_like} THEN 80
            WHEN {filename_like} THEN 70
            WHEN {tag_like} THEN 50
            WHEN {isbn_like} THEN 45
            WHEN {publisher_like} THEN 40
            ELSE 0
        END
        """,
        params,
    )


def _like_any_sql(column: str, terms: list[str], params: list[Any]) -> str:
    parts = []
    for term in terms:
        parts.append(f"{column} LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(term)}%")
    return "(" + " OR ".join(parts) + ")"


def _exact_any_sql(column: str, terms: list[str], params: list[Any]) -> str:
    parts = []
    for term in terms:
        parts.append(f"{column} = ?")
        params.append(term)
    return "(" + " OR ".join(parts) + ")"


def _search_terms(value: str) -> list[str]:
    terms: list[str] = []
    for group in _search_term_groups(value):
        for term in group:
            if term not in terms:
                terms.append(term)
    return terms[:32]


def _search_term_groups(value: str) -> list[list[str]]:
    query = " ".join(str(value or "").split()).strip()
    if not query:
        return []
    return [_search_term_variants(term) for term in query.split(" ") if term]


def _search_term_variants(value: str) -> list[str]:
    terms: list[str] = []

    def add(term: str) -> None:
        term = " ".join(str(term or "").split()).strip()
        if term and term not in terms:
            terms.append(term)

    add(value)
    add(value.translate(str.maketrans({"鲁": "魯", "呐": "吶"})))
    add(value.translate(str.maketrans({"魯": "鲁", "吶": "呐"})))

    aliases = {
        "鲁迅": ["魯迅", "Lu Xun"],
        "魯迅": ["鲁迅", "Lu Xun"],
        "呐喊": ["吶喊", "Ne Han"],
        "吶喊": ["呐喊", "Ne Han"],
    }
    for source, replacements in aliases.items():
        if source in value:
            for replacement in replacements:
                add(value.replace(source, replacement))
    return terms[:16]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _coerce_tags(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        tags = [item.strip() for item in value.split(",")]
    else:
        tags = [str(item).strip() for item in value]
    return [tag for tag in tags if tag]


def _chunks(items: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _book_to_dict(row: sqlite3.Row, tags: list[str]) -> dict[str, Any]:
    result = dict(row)
    result["tags"] = tags
    if not result.get("description"):
        result["description"] = build_simple_description(
            str(result.get("title") or result.get("filename") or ""),
            str(result.get("authors") or ""),
            tags,
            str(result.get("ext") or ""),
        )
    result["description"] = _short_description(str(result.get("description") or ""))
    result["douban_rating_label"] = _douban_rating_label(result)
    result["cover_url"] = f"/api/cover?id={result['id']}" if _has_local_cover(result.get("cover_path", "")) else ""
    result["size_label"] = format_bytes(int(result.get("size") or 0))
    return result


def _prefer(new_value: Any, old_value: Any) -> str:
    text = str(new_value or "").strip()
    return text if text else str(old_value or "")


def _prefer_title(new_value: Any, old_value: Any) -> str:
    new_text = str(new_value or "").strip()
    old_text = str(old_value or "").strip()
    if not new_text or new_text == UNKNOWN_TITLE:
        return old_text
    if not old_text:
        return new_text
    old_bad = is_suspicious_title(old_text) or old_text == UNKNOWN_TITLE
    new_bad = is_suspicious_title(new_text) or new_text == UNKNOWN_TITLE
    if old_bad:
        return new_text if not new_bad else old_text
    if new_bad:
        return old_text
    if _has_chinese(new_text) and not _has_chinese(old_text):
        return new_text
    return new_text


def _prefer_author(new_value: Any, old_value: Any) -> str:
    new_text = str(new_value or "").strip()
    old_text = str(old_value or "").strip()
    if not new_text or new_text == UNKNOWN_AUTHOR:
        return old_text
    if not old_text:
        return new_text
    old_bad = is_suspicious_author(old_text)
    new_bad = is_suspicious_author(new_text)
    if old_bad:
        return new_text if not new_bad else old_text
    if new_bad:
        return old_text
    if _has_chinese(new_text) and not _has_chinese(old_text):
        return new_text
    return new_text


def _has_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value or ""))


def _douban_status(metadata: dict[str, Any], row: sqlite3.Row) -> str:
    new_rating = str(metadata.get("douban_rating") or "").strip()
    old_rating = str(row["douban_rating"] or "").strip()
    if new_rating:
        return str(metadata.get("douban_rating_status") or "已获取")
    if old_rating:
        return str(row["douban_rating_status"] or "已获取")
    return str(metadata.get("douban_rating_status") or row["douban_rating_status"] or "")


def _douban_rating_label(book: dict[str, Any]) -> str:
    rating = str(book.get("douban_rating") or "").strip()
    if rating:
        return rating
    return str(book.get("douban_rating_status") or "未找到")


def _short_description(value: str, limit: int = 150) -> str:
    text = clean_description(value, limit=limit + 1)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _has_local_cover(path_value: Any) -> bool:
    text = str(path_value or "")
    if not text or text.startswith("epub:"):
        return False
    path = Path(text)
    return path.exists() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
