from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bookvault import database  # noqa: E402
from bookvault.config import BACKUP_DIR, DB_PATH  # noqa: E402
from bookvault.metadata import (  # noqa: E402
    UNKNOWN_AUTHOR,
    UNKNOWN_TITLE,
    build_simple_description,
    extract_book_metadata,
    is_suspicious_author,
    is_suspicious_title,
)
from bookvault.tagger import merge_tags, refine_tags  # noqa: E402


REMOTE_PENDING = "待查询"


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair suspicious BookVault metadata safely")
    parser.add_argument("--ids", default="", help="Comma-separated book ids to inspect or repair")
    parser.add_argument("--limit", type=int, default=50, help="Maximum suspicious rows to inspect when --ids is omitted")
    parser.add_argument("--apply", action="store_true", help="Apply changes. Without this flag, only prints a dry-run")
    parser.add_argument("--all", action="store_true", help="Inspect every active row instead of only suspicious rows")
    parser.add_argument(
        "--allow-title-on-author-bad",
        action="store_true",
        help="Also replace a non-suspicious title when the author is corrupt and local extraction finds a different title",
    )
    parser.add_argument(
        "--allow-non-chinese-title",
        action="store_true",
        help="Allow replacing a bad title with a non-Chinese title. By default only Chinese titles or 待识别书名 are accepted",
    )
    parser.add_argument(
        "--authors-only",
        action="store_true",
        help="Only repair suspicious author fields and matching placeholder descriptions; never change titles or tags",
    )
    parser.add_argument(
        "--refresh-description",
        action="store_true",
        help="Refresh description from local metadata when it is usable; safest with --ids",
    )
    parser.add_argument(
        "--strict-titles",
        action="store_true",
        help="Only accept conservative Chinese title repairs for obviously corrupt current titles",
    )
    parser.add_argument("--sample", type=int, default=80, help="Maximum changed rows to print in the preview output")
    args = parser.parse_args()

    database.init_db()
    rows = _load_rows(args.ids)
    if not args.all:
        rows = [row for row in rows if _needs_repair(row)]
    if args.limit > 0 and not args.ids:
        rows = rows[: args.limit]

    previews: list[dict[str, Any]] = []
    for row in rows:
        previews.append(
            _preview_repair(
                row,
                allow_title_on_author_bad=args.allow_title_on_author_bad,
                allow_non_chinese_title=args.allow_non_chinese_title,
                authors_only=args.authors_only,
                refresh_description=args.refresh_description,
                strict_titles=args.strict_titles,
            )
        )

    changed = [item for item in previews if item["changes"]]
    _print_preview(previews, changed, applying=args.apply, sample=args.sample)

    if not args.apply:
        print("\nDRY-RUN: 未修改数据库。确认无误后再加 --apply。")
        return 0

    if not changed:
        print("\n没有需要应用的修改。")
        return 0

    backup_path = _backup_database()
    print(f"\n已先备份数据库：{backup_path}")
    _apply_changes(changed)
    print(f"已应用 {len(changed)} 条元数据修复。源文件路径和文件本身未改动。")
    return 0


def _load_rows(ids_text: str) -> list[sqlite3.Row]:
    with database.connect() as conn:
        if ids_text.strip():
            ids = [int(part.strip()) for part in ids_text.split(",") if part.strip()]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            return conn.execute(
                f"SELECT * FROM books WHERE status = 'active' AND id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()
        return conn.execute("SELECT * FROM books WHERE status = 'active' ORDER BY id").fetchall()


def _needs_repair(row: sqlite3.Row) -> bool:
    title = str(row["title"] or "")
    authors = str(row["authors"] or "")
    return _bad_title(title) or _bad_author(authors)


def _bad_title(title: str) -> bool:
    return title == UNKNOWN_TITLE or is_suspicious_title(title)


def _bad_author(authors: str) -> bool:
    return bool(authors and authors != UNKNOWN_AUTHOR and is_suspicious_author(authors))


def _preview_repair(
    row: sqlite3.Row,
    *,
    allow_title_on_author_bad: bool,
    allow_non_chinese_title: bool,
    authors_only: bool,
    refresh_description: bool,
    strict_titles: bool,
) -> dict[str, Any]:
    old = {
        "title": str(row["title"] or ""),
        "authors": str(row["authors"] or ""),
        "description": str(row["description"] or ""),
        "language": str(row["language"] or ""),
        "publisher": str(row["publisher"] or ""),
        "published_year": str(row["published_year"] or ""),
        "isbn": str(row["isbn"] or ""),
        "cover_path": str(row["cover_path"] or ""),
        "tags_text": str(row["tags_text"] or ""),
    }
    title_bad = _bad_title(old["title"])
    author_bad = _bad_author(old["authors"])
    changes: dict[str, str] = {}
    reasons: list[str] = []

    try:
        metadata = extract_book_metadata(str(row["path"]), extract_text=True)
    except Exception as exc:
        return {
            "id": int(row["id"]),
            "filename": str(row["filename"] or ""),
            "path": str(row["path"] or ""),
            "old": old,
            "new": old,
            "changes": {},
            "reasons": [f"提取失败：{str(exc)[:120]}"],
        }

    new_title = str(metadata.get("title") or "").strip()
    new_authors = str(metadata.get("authors") or "").strip()
    if not authors_only:
        title_allowed = _title_allowed(new_title, allow_non_chinese_title=allow_non_chinese_title)
        if strict_titles and not _strict_title_change_allowed(old["title"], new_title):
            title_allowed = False
        if title_bad and new_title and new_title != old["title"] and title_allowed:
            changes["title"] = new_title
            reasons.append("书名疑似乱码/编号/章节名")
        elif allow_title_on_author_bad and author_bad and _good_title(new_title) and new_title != old["title"] and title_allowed:
            changes["title"] = new_title
            reasons.append("作者异常且本地正文/元数据给出不同书名")

    final_title_for_author = changes.get("title", old["title"])
    if (
        author_bad
        and new_authors
        and new_authors != old["authors"]
        and not (new_authors == old["title"] and "title" not in changes)
        and new_authors != final_title_for_author
        and (new_authors == UNKNOWN_AUTHOR or not _bad_author(new_authors))
    ):
        changes["authors"] = new_authors
        reasons.append("作者疑似乱码/编号/随机字符")

    if not authors_only:
        for field in ("language", "publisher", "published_year", "isbn", "cover_path"):
            value = str(metadata.get(field) or "").strip()
            if value and not old[field]:
                changes[field] = value

        merged_tags = refine_tags(
            changes.get("title", old["title"]),
            changes.get("authors", old["authors"]),
            changes.get("description", old["description"]),
            merge_tags((old["tags_text"] or "").split(","), metadata.get("tags") or []),
        )
        new_tags_text = ", ".join(merged_tags)
        if new_tags_text and new_tags_text != old["tags_text"]:
            changes["tags_text"] = new_tags_text

    new_description = str(metadata.get("description") or "").strip()
    final_title = changes.get("title", old["title"])
    final_authors = changes.get("authors", old["authors"])
    final_tags = [tag.strip() for tag in changes.get("tags_text", old["tags_text"]).split(",") if tag.strip()]
    if not authors_only and "title" in changes:
        if _metadata_description_usable(new_description, final_title):
            changes["description"] = new_description
        else:
            changes["description"] = build_simple_description(final_title, final_authors, final_tags, str(row["ext"] or ""))
    elif refresh_description and _metadata_description_usable(new_description, final_title) and new_description != old["description"]:
        changes["description"] = new_description
        reasons.append("刷新本地简介")
    elif refresh_description and _description_seems_unrelated(old["description"], final_title):
        changes["description"] = build_simple_description(final_title, final_authors, final_tags, str(row["ext"] or ""))
        reasons.append("重建简短简介")
    elif (_description_is_placeholder(old["description"], old["title"]) or not old["description"]) and changes:
        changes["description"] = build_simple_description(final_title, final_authors, final_tags, str(row["ext"] or ""))

    new = dict(old)
    new.update(changes)
    return {
        "id": int(row["id"]),
        "filename": str(row["filename"] or ""),
        "path": str(row["path"] or ""),
        "old": old,
        "new": new,
        "changes": changes,
        "reasons": reasons,
    }


def _good_title(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text and text != UNKNOWN_TITLE and not is_suspicious_title(text))


def _title_allowed(value: str, *, allow_non_chinese_title: bool) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text == UNKNOWN_TITLE:
        return True
    if allow_non_chinese_title:
        return _good_title(text)
    return _good_title(text) and bool(re.search(r"[\u4e00-\u9fff]", text))


def _strict_title_change_allowed(old_title: str, new_title: str) -> bool:
    old = str(old_title or "").strip()
    new = str(new_title or "").strip()
    if not _title_allowed(new, allow_non_chinese_title=False):
        return False
    if len(new) > 32:
        return False
    if re.search(r"[［］\\[\\]【】？?！!]", new):
        return False
    if re.search(r"(作者|著|译|目录|简介|正文|第一章|第[一二三四五六七八九十百千万零〇\\d]+[章节回集]|\\bby\\b|www\\.|https?://)", new, flags=re.I):
        return False
    if re.search(r"[。；;]", new):
        return False
    if len(re.findall(r"[，,、]", new)) > 1:
        return False
    lower = old.lower()
    old_obviously_bad = (
        old == UNKNOWN_TITLE
        or "�" in old
        or re.search(r"[ÃÂÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõøùúûüýþÿ]", old)
        or re.search(r"https?://|www\\.", lower)
        or re.fullmatch(r"\\d+(?:[ _-]\\d+)*", old)
        or re.fullmatch(r"[A-Z0-9_\\-]{8,}", old)
        or re.search(r"\\.(gif|jpg|jpeg|png|webp)\\b", lower)
    )
    return bool(old_obviously_bad)


def _description_is_placeholder(description: str, title: str) -> bool:
    text = str(description or "")
    if not text:
        return True
    return text.startswith(f"《{title}》是一本") and "本地文件格式为" in text


def _metadata_description_usable(description: str, title: str) -> bool:
    text = str(description or "").strip()
    if not text or text.startswith("《"):
        return False
    if "#" in text and text.count("#") >= 3:
        return False
    if re.search(r"目\s*录", text):
        return False
    if title and title != UNKNOWN_TITLE and title in text:
        return True
    return len(text) >= 30 and bool(re.search(r"[\u4e00-\u9fff]", text))


def _description_seems_unrelated(description: str, title: str) -> bool:
    text = str(description or "")
    if not text:
        return True
    if not title or title == UNKNOWN_TITLE:
        return False
    return title not in text


def _print_preview(previews: list[dict[str, Any]], changed: list[dict[str, Any]], *, applying: bool, sample: int) -> None:
    mode = "APPLY" if applying else "DRY-RUN"
    print(f"{mode}: inspected={len(previews)} changed={len(changed)}")
    printed = 0
    for item in previews:
        if not item["changes"]:
            continue
        if printed >= sample:
            continue
        printed += 1
        print(f"\nID {item['id']} | {item['filename']}")
        if item["reasons"]:
            print(f"原因: {'；'.join(item['reasons'])}")
        for field, value in item["changes"].items():
            old_value = item["old"].get(field, "")
            print(f"  {field}: {_preview_value(old_value)!r} -> {_preview_value(value)!r}")
    omitted = max(0, len(changed) - printed)
    if omitted:
        print(f"\n... 还有 {omitted} 条修改未打印；可用 --sample 调整预览数量。")


def _backup_database() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"bookvault_before_metadata_repair_{time.strftime('%Y%m%d_%H%M%S')}.sqlite3"
    with database.connect() as source:
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
    return backup_path


def _preview_value(value: object, limit: int = 180) -> str:
    text = str(value or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _apply_changes(changed: list[dict[str, Any]]) -> None:
    now = time.time()
    with database.connect() as conn:
        for item in changed:
            changes = dict(item["changes"])
            book_id = int(item["id"])
            title_changed = "title" in changes and changes["title"] != item["old"]["title"]
            update_values: dict[str, Any] = {
                "id": book_id,
                "updated_at": now,
                "last_scanned_at": now,
                "title": changes.get("title", item["old"]["title"]),
                "authors": changes.get("authors", item["old"]["authors"]),
                "description": changes.get("description", item["old"]["description"]),
                "language": changes.get("language", item["old"]["language"]),
                "publisher": changes.get("publisher", item["old"]["publisher"]),
                "published_year": changes.get("published_year", item["old"]["published_year"]),
                "isbn": changes.get("isbn", item["old"]["isbn"]),
                "cover_path": changes.get("cover_path", item["old"]["cover_path"]),
                "tags_text": changes.get("tags_text", item["old"]["tags_text"]),
            }
            remote_sql = ""
            if title_changed:
                remote_sql = """
                    remote_source = '',
                    remote_url = '',
                    douban_rating = '',
                    douban_url = '',
                    douban_rating_status = :douban_rating_status,
                    remote_lookup_version = 0,
                """
                update_values["douban_rating_status"] = REMOTE_PENDING

            conn.execute(
                f"""
                UPDATE books
                SET title = :title,
                    authors = :authors,
                    description = :description,
                    language = :language,
                    publisher = :publisher,
                    published_year = :published_year,
                    isbn = :isbn,
                    cover_path = :cover_path,
                    tags_text = :tags_text,
                    {remote_sql}
                    updated_at = :updated_at,
                    last_scanned_at = :last_scanned_at
                WHERE id = :id
                """,
                update_values,
            )
            database.replace_tags(conn, book_id, update_values["tags_text"].split(","))


if __name__ == "__main__":
    raise SystemExit(main())
