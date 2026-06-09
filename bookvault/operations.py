from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from . import database
from .config import DESKTOP_OUTPUT_DIR


def desktop_dir() -> Path:
    if DESKTOP_OUTPUT_DIR:
        return DESKTOP_OUTPUT_DIR
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        desktop = Path(user_profile) / "Desktop"
        if desktop.exists():
            return desktop
    return Path.home() / "Desktop"


def copy_to_desktop(book_ids: list[int]) -> dict[str, Any]:
    books = database.get_books_by_ids(book_ids)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target_root = desktop_dir() / "BookVault_Copies" / stamp
    target_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    errors: list[str] = []
    for book in books:
        source = Path(book["path"])
        if not source.exists():
            errors.append(f"文件不存在：{source}")
            continue
        target = _unique_path(target_root / source.name)
        try:
            shutil.copy2(source, target)
            copied += 1
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    return {
        "copied": copied,
        "requested": len(books),
        "target": str(target_root),
        "errors": errors[:50],
    }


def delete_preview(book_ids: list[int]) -> dict[str, Any]:
    ids = _unique_ids(book_ids)
    books = database.get_books_by_ids(ids)
    if not books:
        raise ValueError("没有可执行安全删除的书籍")
    if len(books) != len(ids):
        found_ids = {int(book["id"]) for book in books}
        missing_ids = [book_id for book_id in ids if book_id not in found_ids]
        raise ValueError(f"部分书籍记录不存在，未创建操作：{missing_ids[:10]}")
    validation_errors = [_source_state_error(book) for book in books]
    validation_errors = [error for error in validation_errors if error]
    if validation_errors:
        raise ValueError(f"源文件状态与索引不一致，未创建操作：{validation_errors[0]}")

    total_size = sum(int(book.get("size") or 0) for book in books)
    count = len(books)
    phrase = f"安全删除 {count} 本书"
    operation_id = uuid.uuid4().hex
    database.create_operation_batch(
        operation_id,
        kind="safe_delete",
        confirm_phrase=phrase,
        items=books,
    )
    return {
        "operation_id": operation_id,
        "count": count,
        "total_size": total_size,
        "total_size_label": database.format_bytes(total_size),
        "confirm_phrase": phrase,
        "sample": [
            {
                "id": book["id"],
                "title": book.get("title") or book.get("filename"),
                "path": book["path"],
                "size_label": book.get("size_label"),
            }
            for book in books[:12]
        ],
    }


def safe_delete(operation_id: str, confirm_text: str) -> dict[str, Any]:
    operation_id = str(operation_id or "").strip()
    if not operation_id:
        raise ValueError("缺少安全删除 operation_id")
    batch = database.get_operation_batch(operation_id)
    if not batch or batch.get("kind") != "safe_delete":
        raise ValueError("安全删除操作不存在")
    if batch.get("status") != "pending":
        raise ValueError(f"安全删除操作当前状态不可执行：{batch.get('status')}")
    if confirm_text != batch["confirm_phrase"]:
        raise ValueError("确认短语不匹配，未执行删除")

    items = list(batch.get("items") or [])
    validation_errors = _validate_frozen_items(items)
    if validation_errors:
        message = "；".join(validation_errors[:10])
        database.finish_operation_batch(operation_id, status="rejected", error=message)
        raise ValueError(f"源文件或索引状态已变化，操作已拒绝：{validation_errors[0]}")
    if not database.claim_operation_batch(operation_id):
        raise ValueError("安全删除操作已被其他请求领取或状态已变化")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    quarantine_root = desktop_dir() / "BookVault_Quarantine" / f"{stamp}_{operation_id[:8]}"
    quarantine_root.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    moved = 0
    errors: list[str] = []
    for item in items:
        source = Path(str(item["path"]))
        state_error = _source_state_error(item)
        if state_error:
            errors.append(state_error)
            database.update_operation_item(
                operation_id,
                int(item["book_id"]),
                status="failed",
                error=state_error,
            )
            break
        target = _unique_path(quarantine_root / _safe_relative_path(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source), str(target))
            if not database.set_quarantined_if_unchanged(item, str(target)):
                try:
                    if target.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(source))
                except Exception as rollback_exc:
                    raise RuntimeError(f"数据库状态变化且文件回移失败：{rollback_exc}") from rollback_exc
                raise RuntimeError("数据库记录已变化，文件已回移，操作停止")
            database.update_operation_item(
                operation_id,
                int(item["book_id"]),
                status="moved",
                quarantine_path=str(target),
            )
            moved += 1
            manifest.append(
                {
                    "id": item["book_id"],
                    "title": item.get("title"),
                    "original_path": str(source),
                    "quarantine_path": str(target),
                    "size": item.get("size"),
                    "mtime": item.get("mtime"),
                }
            )
        except Exception as exc:
            message = f"{source}: {exc}"
            errors.append(message)
            database.update_operation_item(
                operation_id,
                int(item["book_id"]),
                status="failed",
                error=message,
            )
            break

    manifest_path = quarantine_root / "manifest.json"
    try:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        errors.append(f"写入操作清单失败：{exc}")
    database.finish_operation_batch(
        operation_id,
        status="failed" if errors else "completed",
        output_path=str(quarantine_root),
        error="；".join(errors[:10]),
    )
    return {
        "operation_id": operation_id,
        "moved": moved,
        "requested": len(items),
        "quarantine": str(quarantine_root),
        "manifest": str(manifest_path),
        "errors": errors[:50],
    }


def _validate_frozen_items(items: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    with database.connect() as conn:
        for item in items:
            row = conn.execute(
                "SELECT id, path, size, mtime, status FROM books WHERE id = ?",
                (int(item["book_id"]),),
            ).fetchone()
            if not row:
                errors.append(f"记录不存在：ID {item['book_id']}")
                continue
            if (
                str(row["path"]) != str(item["path"])
                or int(row["size"]) != int(item["size"])
                or abs(float(row["mtime"]) - float(item["mtime"])) >= 0.001
                or str(row["status"]) != "active"
            ):
                errors.append(f"索引状态变化：ID {item['book_id']}")
                continue
            source_error = _source_state_error(item)
            if source_error:
                errors.append(source_error)
    return errors


def _source_state_error(item: dict[str, Any]) -> str:
    path = Path(str(item["path"]))
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return f"文件不存在：{path}"
    except OSError as exc:
        return f"无法读取文件状态：{path}: {exc}"
    if not path.is_file():
        return f"源路径不是文件：{path}"
    if int(stat_result.st_size) != int(item["size"]):
        return f"文件大小变化：{path}"
    if abs(float(stat_result.st_mtime) - float(item["mtime"])) >= 0.001:
        return f"文件修改时间变化：{path}"
    return ""


def _unique_ids(book_ids: list[int]) -> list[int]:
    return list(dict.fromkeys(int(item) for item in book_ids))


def _safe_relative_path(path: Path) -> Path:
    resolved = path.resolve()
    drive = resolved.drive.replace(":", "") or "drive"
    parts = [drive, *[part for part in resolved.parts[1:] if part not in {"\\", "/"}]]
    clean_parts = [_sanitize_filename(part) for part in parts if part]
    return Path(*clean_parts)


def _sanitize_filename(value: str) -> str:
    return "".join("_" if char in '<>:"/\\|?*' else char for char in value).strip() or "_"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
