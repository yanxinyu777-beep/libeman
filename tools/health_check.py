from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bookvault import database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run BookVault stability checks")
    parser.add_argument("--live-url", default="", help="Optional running app URL, for example http://127.0.0.1:8765")
    parser.add_argument("--skip-source-check", action="store_true", help="Skip checking that every source file still exists")
    args = parser.parse_args()

    checks: list[tuple[str, bool, str]] = []
    database.init_db()

    checks.append(_check_frontend_columns())
    checks.extend(_check_book_invariants())
    checks.extend(_check_large_id_paths())
    if not args.skip_source_check:
        checks.append(_check_source_links())
    if args.live_url:
        checks.extend(_check_live_api(args.live_url.rstrip("/")))

    failed = [item for item in checks if not item[1]]
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}")
    return 1 if failed else 0


def _check_frontend_columns() -> tuple[str, bool, str]:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    required = ["<th>书籍</th>", "<th>豆瓣评分</th>", "<th>出版社</th>", "<th>标签</th>", "<th>格式</th>", "<th>大小</th>"]
    missing = [item for item in required if item not in html]
    if "<th>路径</th>" in html:
        return ("frontend_columns", False, "路径列仍在表头中")
    if missing:
        return ("frontend_columns", False, f"缺少表头：{', '.join(missing)}")
    return ("frontend_columns", True, "表头符合当前需求")


def _check_book_invariants() -> list[tuple[str, bool, str]]:
    items: list[dict[str, Any]] = []
    page = 1
    total = 0
    while True:
        payload = database.list_books({}, page=page, page_size=200)
        total = int(payload["total"])
        items.extend(payload["items"])
        if len(items) >= total or not payload["items"]:
            break
        page += 1
    long_desc = [book for book in items if len(str(book.get("description") or "")) > 150]
    uncategorized = [
        book for book in items
        if "未分类" in str(book.get("description") or "") or "未分类" in ",".join(book.get("tags") or [])
    ]
    missing_rating_label = [book for book in items if not str(book.get("douban_rating_label") or "").strip()]
    return [
        ("description_length", not long_desc, f"检查 {len(items)} 条当前页记录，超长 {len(long_desc)} 条"),
        ("no_uncategorized_label", not uncategorized, f"检查 {len(items)} 条当前页记录，命中 {len(uncategorized)} 条"),
        ("rating_label_present", not missing_rating_label, f"检查 {len(items)} 条当前页记录，缺失 {len(missing_rating_label)} 条"),
    ]


def _check_large_id_paths() -> list[tuple[str, bool, str]]:
    ids = database.select_book_ids({"status": "active"}, limit=20)
    checks: list[tuple[str, bool, str]] = []
    if ids:
        repeated_ids = [ids[index % len(ids)] for index in range(1200)]
        books = database.get_books_by_ids(repeated_ids)
        checks.append(("large_get_books_by_ids", len(books) == len(repeated_ids), f"请求 {len(repeated_ids)} 条，返回 {len(books)} 条"))
    else:
        checks.append(("large_get_books_by_ids", True, "数据库暂无 active 记录，跳过"))

    try:
        excluded = list(range(1, 1500))
        selected = database.select_book_ids({"status": "active"}, exclude_ids=excluded, limit=5)
        checks.append(("large_exclude_selection", True, f"大排除列表查询返回 {len(selected)} 条"))
    except Exception as exc:
        checks.append(("large_exclude_selection", False, str(exc)))
    return checks


def _check_source_links() -> tuple[str, bool, str]:
    with database.connect() as conn:
        rows = conn.execute("SELECT id, path FROM books WHERE status = 'active'").fetchall()
    missing = []
    for row in rows:
        if not Path(str(row["path"])).exists():
            missing.append(int(row["id"]))
            if len(missing) >= 10:
                break
    if not rows:
        return ("source_links", False, "数据库没有 active 记录")
    if missing:
        return ("source_links", False, f"发现源文件缺失，样例 ID：{missing}")
    return ("source_links", True, f"{len(rows)} 条 active 记录均能找到源文件")


def _check_live_api(base_url: str) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    try:
        books = _fetch_json(f"{base_url}/api/books?page_size=10")
        jobs = _fetch_json(f"{base_url}/api/jobs")
        checks.append(("live_books_api", isinstance(books.get("items"), list), f"返回 {len(books.get('items') or [])} 条"))
        checks.append(("live_jobs_api", isinstance(jobs.get("jobs"), list), f"返回 {len(jobs.get('jobs') or [])} 个任务"))
    except Exception as exc:
        checks.append(("live_api", False, str(exc)))
    return checks


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
