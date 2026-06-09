from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from bookvault import database, dialogs, jobs, operations, workers
from bookvault.config import DEFAULT_PAGE_SIZE, STATIC_DIR


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class BookVaultHandler(BaseHTTPRequestHandler):
    server_version = "BookVault/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json_response({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._read_json()
            self._handle_api_post(parsed.path, body)
        except ValueError as exc:
            self._json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[BookVault] {self.address_string()} - {fmt % args}")

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/books":
            filters = _filters_from_query(query)
            page = int(_first(query, "page", "1"))
            page_size = int(_first(query, "page_size", str(DEFAULT_PAGE_SIZE)))
            self._json_response(database.list_books(filters, page=page, page_size=page_size))
        elif path == "/api/cover":
            self._serve_cover(query)
        elif path == "/api/tags":
            self._json_response({"tags": database.get_tags()})
        elif path == "/api/formats":
            self._json_response({"formats": database.get_formats()})
        elif path == "/api/stats":
            stats = database.get_stats()
            stats["remote_pause"] = workers.remote_lookup_pause_status()
            self._json_response(stats)
        elif path == "/api/jobs":
            self._json_response({"jobs": jobs.get_jobs()})
        else:
            self._json_response({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _serve_cover(self, query: dict[str, list[str]]) -> None:
        book_id = _first(query, "id", "")
        if not book_id.isdigit():
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        books = database.get_books_by_ids([int(book_id)])
        if not books:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        cover_path = Path(str(books[0].get("cover_path") or ""))
        if not cover_path.exists() or cover_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(cover_path))[0] or "image/jpeg"
        data = cover_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_api_post(self, path: str, body: dict[str, object]) -> None:
        if path == "/api/scan/start":
            root = str(body.get("root") or "").strip()
            extract_text = bool(body.get("extract_text", True))
            self._json_response({"job": workers.start_scan(root, extract_text=extract_text)})
        elif path == "/api/dialog/folder":
            initial_dir = str(body.get("initial_dir") or body.get("root") or "").strip()
            folder = dialogs.choose_folder(initial_dir)
            self._json_response({"path": folder})
        elif path == "/api/delete/preview":
            ids = _resolve_selection(body)
            self._json_response(operations.delete_preview(ids))
        elif path == "/api/delete/execute":
            operation_id = str(body.get("operation_id") or "").strip()
            confirm_text = str(body.get("confirm_text") or "")
            self._json_response(operations.safe_delete(operation_id, confirm_text))
        elif path == "/api/copy-desktop":
            ids = _resolve_selection(body)
            self._json_response(operations.copy_to_desktop(ids))
        elif path == "/api/tags/add":
            ids = _resolve_selection(body)
            raw_tags = body.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = [item.strip() for item in raw_tags.split(",")]
            changed = database.add_manual_tags(ids, raw_tags)  # type: ignore[arg-type]
            self._json_response({"changed": changed})
        else:
            self._json_response({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _serve_static(self, request_path: str) -> None:
        if request_path in {"", "/"}:
            target = STATIC_DIR / "index.html"
        else:
            relative = Path(unquote(request_path.lstrip("/")))
            target = (STATIC_DIR / relative).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _json_response(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _resolve_selection(body: dict[str, object], default_limit: int | None = None) -> list[int]:
    selection = body.get("selection")
    if not isinstance(selection, dict):
        selection = body

    all_matching = bool(selection.get("allMatching") or selection.get("all_matching"))
    filters = body.get("filters") if isinstance(body.get("filters"), dict) else {}
    exclude = selection.get("excludeIds") or selection.get("exclude_ids") or []
    if all_matching:
        limit = body.get("limit") or default_limit
        return database.select_book_ids(filters, exclude_ids=exclude, limit=int(limit) if limit else None)  # type: ignore[arg-type]

    raw_ids = selection.get("ids") or body.get("ids") or []
    if not isinstance(raw_ids, list):
        raise ValueError("缺少选择的书籍")
    ids = [int(item) for item in raw_ids if str(item).isdigit()]
    explicit_limit = body.get("limit")
    if explicit_limit:
        ids = ids[: int(explicit_limit)]
    return ids


def _filters_from_query(query: dict[str, list[str]]) -> dict[str, object]:
    return {
        "query": _first(query, "query", ""),
        "tags": _first(query, "tags", ""),
        "format": _first(query, "format", ""),
        "status": _first(query, "status", "active"),
        "sort": _first(query, "sort", "updated"),
        "order": _first(query, "order", "desc"),
    }


def _first(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


def _resume_pending_remote_lookup() -> dict[str, object] | None:
    if workers.remote_lookup_disabled():
        return None
    return workers.start_pending_remote_lookup()


def main() -> None:
    parser = argparse.ArgumentParser(description="BookVault local library manager")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    database.init_db()
    interrupted_jobs = jobs.initialize_after_restart()
    if interrupted_jobs:
        print(f"BookVault 已标记 {interrupted_jobs} 个重启前未完成任务为中断")
    backup_path = database.backup_database_once_per_day()
    if backup_path:
        print(f"BookVault 数据库备份：{backup_path}")
    reused_remote = database.reuse_remote_metadata_for_identical_copies()
    if reused_remote:
        print(f"BookVault 已为 {reused_remote} 份完全一致的副本复用豆瓣评分")
    pending_job = _resume_pending_remote_lookup()
    if pending_job:
        print(f"BookVault 已恢复未完成的联网补齐任务：{pending_job['id']}")
    server = ReusableThreadingHTTPServer((args.host, args.port), BookVaultHandler)
    print(f"BookVault 已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("BookVault 已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
