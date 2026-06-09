from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


TEST_ROOT = Path(tempfile.mkdtemp(prefix="bvtests"))
DATA_DIR = TEST_ROOT / "data"
DB_PATH = DATA_DIR / "test.sqlite3"
DESKTOP_DIR = TEST_ROOT / "desktop"
SOURCE_DIR = TEST_ROOT / "sources"

os.environ["BOOKVAULT_DATA_DIR"] = str(DATA_DIR)
os.environ["BOOKVAULT_DB_PATH"] = str(DB_PATH)
os.environ["BOOKVAULT_DESKTOP_DIR"] = str(DESKTOP_DIR)
os.environ["BOOKVAULT_DISABLE_REMOTE"] = "1"


def reset_workspace() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in (DESKTOP_DIR, SOURCE_DIR):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

    from bookvault import database

    database.init_db()
    conn = database.connect()
    try:
        conn.execute("DELETE FROM operation_items")
        conn.execute("DELETE FROM operation_batches")
        conn.execute("DELETE FROM background_jobs")
        conn.execute("DELETE FROM book_tags")
        conn.execute("DELETE FROM books")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
    finally:
        conn.close()


def add_book(
    filename: str,
    *,
    title: str,
    authors: str = "",
    description: str = "",
    content: bytes = b"book",
    metadata: dict[str, Any] | None = None,
) -> tuple[int, Path]:
    from bookvault import database

    path = SOURCE_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    values: dict[str, Any] = {
        "title": title,
        "authors": authors,
        "description": description,
        "source": "test",
        "tags": [],
    }
    values.update(metadata or {})
    book_id = database.upsert_book(path, path.stat(), values)
    return book_id, path


atexit.register(lambda: shutil.rmtree(TEST_ROOT, ignore_errors=True))
