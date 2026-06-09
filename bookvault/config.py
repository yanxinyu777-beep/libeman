from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "BookVault"
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


_ENV_DATA_DIR = _env_path("BOOKVAULT_DATA_DIR")
_ENV_DB_PATH = _env_path("BOOKVAULT_DB_PATH")
DATA_DIR = _ENV_DATA_DIR or (_ENV_DB_PATH.parent if _ENV_DB_PATH else BASE_DIR / "data")
DB_PATH = _ENV_DB_PATH or DATA_DIR / "bookvault.sqlite3"
BACKUP_DIR = DATA_DIR / "backups"
STATIC_DIR = BASE_DIR / "static"
DESKTOP_OUTPUT_DIR = _env_path("BOOKVAULT_DESKTOP_DIR")

SUPPORTED_EXTENSIONS = {
    ".azw",
    ".azw3",
    ".doc",
    ".docx",
    ".epub",
    ".fb2",
    ".md",
    ".mobi",
    ".pdf",
    ".rtf",
    ".txt",
}

SKIP_DIR_NAMES = {
    "$RECYCLE.BIN",
    ".bookvault",
    ".bookvault_trash",
    ".git",
    "__MACOSX",
    "BookVault_Copies",
    "BookVault_Quarantine",
    "System Volume Information",
}

MAX_EXCERPT_CHARS = 1400
MAX_READ_BYTES = 512 * 1024
DEFAULT_PAGE_SIZE = 50
