from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import database, jobs
from .config import DATA_DIR, SKIP_DIR_NAMES, SUPPORTED_EXTENSIONS
from .metadata import extract_book_metadata, is_suspicious_title
from .network import lookup_book_metadata
from .tagger import infer_tags, merge_tags


PROGRESS_INTERVAL_FILES = 100
PROGRESS_INTERVAL_SECONDS = 2.0
REMOTE_LOOKUP_DELAY_SECONDS = 0.7
REMOTE_RETRY_AFTER_SECONDS = 24 * 60 * 60
REMOTE_FAILURE_STREAK_LIMIT = 8
REMOTE_PAUSE_SECONDS = 6 * 60 * 60
REMOTE_PAUSE_PATH = DATA_DIR / "remote_lookup_pause.json"

_REMOTE_LOOKUP_LOCK = threading.Lock()
_REMOTE_QUEUE_LOCK = threading.Lock()
_REMOTE_RESUME_LOCK = threading.Lock()
_REMOTE_BOOK_IDS_IN_FLIGHT: set[int] = set()
_REMOTE_RESUME_TIMER: threading.Timer | None = None
TOUCH_BATCH_SIZE = 1000


def remote_lookup_disabled() -> bool:
    return os.environ.get("BOOKVAULT_DISABLE_REMOTE", "").strip().lower() in {"1", "true", "yes", "on"}


def start_scan(root: str, extract_text: bool = True) -> dict[str, Any]:
    root_path = Path(root).expanduser()
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError("扫描位置不存在或不是文件夹")
    if jobs.has_active("scan"):
        raise ValueError("已有扫描任务正在运行，请等待完成后再开始新的扫描")
    job = jobs.create_job("scan", {"root": str(root_path), "extract_text": extract_text})

    def worker(job_id: str) -> None:
        processed = 0
        found = 0
        skipped = 0
        indexed = 0
        unchanged = 0
        remote_queued = 0
        remote_skipped = 0
        errors = 0
        remote_queue: list[int] = []
        touch_batch: list[tuple[int, Path, Any]] = []
        last_progress = time.monotonic()
        skip_names = {name.lower() for name in SKIP_DIR_NAMES}
        snapshots = database.get_scan_snapshots()

        def flush_touch_batch() -> None:
            nonlocal touch_batch
            if not touch_batch:
                return
            database.touch_books_scan_batch(touch_batch)
            touch_batch = []

        def emit_progress(message: str, force: bool = False) -> None:
            nonlocal last_progress
            now = time.monotonic()
            if not force and processed % PROGRESS_INTERVAL_FILES != 0 and now - last_progress < PROGRESS_INTERVAL_SECONDS:
                return
            jobs.update_job(
                job_id,
                processed=processed,
                found=found,
                skipped=skipped,
                indexed=indexed,
                unchanged=unchanged,
                remote_queued=remote_queued,
                remote_skipped=remote_skipped,
                error_count=errors,
                message=message,
            )
            last_progress = now

        emit_progress(f"正在扫描：{root_path}", force=True)
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [name for name in dirnames if name.lower() not in skip_names and not name.startswith(".")]
            for filename in filenames:
                processed += 1
                file_path = Path(dirpath) / filename
                if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    skipped += 1
                    emit_progress(f"已检查 {processed} 个文件，识别 {found} 本书")
                    continue
                try:
                    stat_result = file_path.stat()
                    found += 1
                    existing = snapshots.get(str(file_path))
                    same_file = bool(existing and _same_file(existing, stat_result))
                    refresh_reason = _local_refresh_reason(existing) if same_file and existing else ""
                    if existing and same_file and not refresh_reason:
                        book_id = int(existing["id"])
                        touch_batch.append((book_id, file_path, stat_result))
                        if len(touch_batch) >= TOUCH_BATCH_SIZE:
                            flush_touch_batch()
                        unchanged += 1
                        if _remote_lookup_due(existing):
                            remote_queue.append(book_id)
                            remote_queued += 1
                        else:
                            remote_skipped += 1
                        emit_progress(
                            f"已检查 {processed} 个文件，识别 {found} 本书；{unchanged} 本未变化已跳过本地解析"
                        )
                        continue

                    metadata = extract_book_metadata(file_path, extract_text=extract_text)
                    if existing and refresh_reason == "missing_tags":
                        metadata["tags"] = merge_tags(
                            metadata.get("tags"),
                            infer_tags(
                                existing.get("title"),
                                existing.get("authors"),
                                existing.get("description"),
                                existing.get("filename"),
                            ),
                        )
                    should_queue_remote = (
                        not existing
                        or not same_file
                        or refresh_reason == "bad_title"
                        or refresh_reason == "missing_tags"
                        or bool(existing and _remote_lookup_due(existing))
                    )
                    if should_queue_remote:
                        metadata["douban_rating_status"] = metadata.get("douban_rating_status") or "待查询"
                    if existing and (not same_file or refresh_reason == "bad_title"):
                        metadata["reset_remote"] = True
                    book_id = database.upsert_book(file_path, stat_result, metadata)
                    if should_queue_remote:
                        remote_queue.append(book_id)
                        remote_queued += 1
                    else:
                        remote_skipped += 1
                    indexed += 1
                    emit_progress(
                        f"正在建立本地索引：{file_path.name}；联网简介和豆瓣评分稍后自动补齐"
                    )
                except Exception as exc:
                    errors += 1
                    jobs.append_error(job_id, f"{file_path}: {exc}")
                    emit_progress(f"已记录 {errors} 个扫描错误，继续处理后续文件")

        flush_touch_batch()
        metadata_job = start_remote_lookup(remote_queue, root=str(root_path))
        suffix = f"；已启动后台补齐 {remote_queued} 本书的简介和豆瓣评分" if metadata_job else ""
        jobs.update_job(
            job_id,
            status="finished",
            processed=processed,
            found=found,
            skipped=skipped,
            indexed=indexed,
            unchanged=unchanged,
            remote_queued=remote_queued,
            remote_skipped=remote_skipped,
            error_count=errors,
            message=f"扫描完成：识别 {found} 本书，本地新增/更新 {indexed} 本，跳过未变化 {unchanged} 本{suffix}",
        )

    jobs.run_background(job["id"], worker)
    return job


def start_remote_lookup(book_ids: list[int], root: str = "") -> dict[str, Any] | None:
    if remote_lookup_disabled():
        return None
    pause = remote_lookup_pause_status()
    if pause.get("active"):
        return None
    ids = _claim_remote_ids(_unique_ids(book_ids))
    if not ids:
        return None
    job = jobs.create_job("metadata", {"count": len(ids), "root": root})

    def worker(job_id: str) -> None:
        try:
            processed = 0
            updated = 0
            skipped = 0
            errors = 0
            failure_streak = 0
            total = len(ids)
            last_progress = time.monotonic()

            def emit_progress(message: str, force: bool = False) -> None:
                nonlocal last_progress
                now = time.monotonic()
                if not force and processed % PROGRESS_INTERVAL_FILES != 0 and now - last_progress < PROGRESS_INTERVAL_SECONDS:
                    return
                jobs.update_job(
                    job_id,
                    processed=processed,
                    total=total,
                    updated=updated,
                    skipped=skipped,
                    error_count=errors,
                    message=message,
                )
                last_progress = now

            emit_progress(f"后台补齐简介和豆瓣评分：{total} 本待处理", force=True)
            for book_id in ids:
                pause = remote_lookup_pause_status()
                if pause.get("active"):
                    _schedule_resume_after_pause(pause)
                    jobs.update_job(
                        job_id,
                        status="finished",
                        processed=processed,
                        total=total,
                        updated=updated,
                        skipped=skipped,
                        error_count=errors,
                        message=f"联网补齐已暂停；剩余记录保留待查询，预计 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(pause.get('until') or 0)))} 后再继续",
                    )
                    return
                processed += 1
                snapshot = database.get_book_remote_snapshot(book_id)
                if not snapshot or not _remote_lookup_due(snapshot):
                    skipped += 1
                    emit_progress(f"后台补齐中：已处理 {processed}/{total} 本")
                    continue

                title = str(snapshot.get("title") or snapshot.get("filename") or "").strip()
                authors = str(snapshot.get("authors") or "").strip()
                filename = str(snapshot.get("filename") or "").strip()
                description = str(snapshot.get("description") or "").strip()
                try:
                    emit_progress(f"正在联网补齐：{title or filename}", force=True)
                    with _REMOTE_LOOKUP_LOCK:
                        remote = lookup_book_metadata(
                            title,
                            authors,
                            filename=filename,
                            local_description=description,
                        )
                        time.sleep(REMOTE_LOOKUP_DELAY_SECONDS)
                except Exception as exc:
                    errors += 1
                    remote = {"douban_rating_status": "查询失败"}
                    jobs.append_error(job_id, f"{title or filename}: {exc}")

                database.update_book_remote(book_id, remote or {"douban_rating_status": "未找到"})
                updated += 1
                if _remote_lookup_failed(remote):
                    failure_streak += 1
                else:
                    failure_streak = 0
                if failure_streak >= REMOTE_FAILURE_STREAK_LIMIT:
                    pause_until = _pause_remote_lookup("豆瓣访问受限或网络连续查询失败，已暂停联网补齐")
                    _schedule_resume_after_pause({"active": True, "until": pause_until})
                    jobs.update_job(
                        job_id,
                        status="finished",
                        processed=processed,
                        total=total,
                        updated=updated,
                        skipped=skipped,
                        error_count=errors,
                        message=f"联网补齐已暂停：连续 {failure_streak} 次查询失败；剩余记录保留待查询，预计 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pause_until))} 后再继续",
                    )
                    return
                emit_progress(f"后台补齐中：已处理 {processed}/{total} 本，已更新 {updated} 本")

            jobs.update_job(
                job_id,
                status="finished",
                processed=processed,
                total=total,
                updated=updated,
                skipped=skipped,
                error_count=errors,
                message=f"后台补齐完成：更新 {updated} 本，跳过 {skipped} 本",
            )
        finally:
            _release_remote_ids(ids)

    jobs.run_background(job["id"], worker)
    return job


def start_pending_remote_lookup(limit: int | None = None) -> dict[str, Any] | None:
    if remote_lookup_disabled():
        return None
    pause = remote_lookup_pause_status()
    if pause.get("active"):
        _schedule_resume_after_pause(pause, limit=limit)
        return None
    ids = database.select_remote_lookup_due_ids(limit=limit)
    if not ids:
        return None
    return start_remote_lookup(ids, root="未完成的联网补齐")


def _same_file(book: dict[str, Any], stat_result: Any) -> bool:
    old_size = int(book.get("size") or -1)
    old_mtime = float(book.get("mtime") or -1)
    return old_size == int(stat_result.st_size) and abs(old_mtime - float(stat_result.st_mtime)) < 0.01


def _remote_lookup_due(book: dict[str, Any]) -> bool:
    rating = str(book.get("douban_rating") or "").strip()
    status = str(book.get("douban_rating_status") or "").strip()
    lookup_version = int(book.get("remote_lookup_version") or 0)
    if not rating and status in {"未找到", "无评分"} and lookup_version < database.REMOTE_LOOKUP_VERSION:
        return True
    if rating or status in {"已获取", "未找到", "无评分"}:
        return False
    if status == "查询失败":
        last_lookup = float(book.get("last_remote_lookup_at") or 0)
        return time.time() - last_lookup >= REMOTE_RETRY_AFTER_SECONDS
    if status == "豆瓣访问受限":
        last_lookup = float(book.get("last_remote_lookup_at") or 0)
        return time.time() - last_lookup >= REMOTE_RETRY_AFTER_SECONDS
    return True


def _local_refresh_reason(book: dict[str, Any]) -> str:
    title = str(book.get("title") or "")
    description = str(book.get("description") or "")
    if is_suspicious_title(title):
        return "bad_title"
    if "尚未分类" in description or "未分类" in str(book.get("tags_text") or ""):
        return "default_description"
    if not str(book.get("tags_text") or "").strip():
        return "missing_tags"
    return ""


def _unique_ids(book_ids: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for raw_id in book_ids:
        book_id = int(raw_id)
        if book_id in seen:
            continue
        seen.add(book_id)
        result.append(book_id)
    return result


def remote_lookup_pause_status() -> dict[str, Any]:
    try:
        data = json.loads(REMOTE_PAUSE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"active": False, "until": 0, "reason": ""}
    until = float(data.get("until") or 0)
    if until <= time.time():
        return {"active": False, "until": until, "reason": str(data.get("reason") or "")}
    return {"active": True, "until": until, "reason": str(data.get("reason") or "")}


def _pause_remote_lookup(reason: str) -> float:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    until = time.time() + REMOTE_PAUSE_SECONDS
    REMOTE_PAUSE_PATH.write_text(
        json.dumps({"until": until, "reason": reason}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return until


def _remote_lookup_failed(remote: dict[str, Any] | None) -> bool:
    status = str((remote or {}).get("douban_rating_status") or "")
    return status in {"查询失败", "豆瓣访问受限"}


def _schedule_resume_after_pause(pause: dict[str, Any], limit: int | None = None) -> None:
    global _REMOTE_RESUME_TIMER
    if remote_lookup_disabled():
        return
    until = float(pause.get("until") or 0)
    delay = max(5.0, until - time.time() + 2.0)

    def resume() -> None:
        global _REMOTE_RESUME_TIMER
        with _REMOTE_RESUME_LOCK:
            _REMOTE_RESUME_TIMER = None
        start_pending_remote_lookup(limit=limit)

    with _REMOTE_RESUME_LOCK:
        if _REMOTE_RESUME_TIMER and _REMOTE_RESUME_TIMER.is_alive():
            return
        _REMOTE_RESUME_TIMER = threading.Timer(delay, resume)
        _REMOTE_RESUME_TIMER.daemon = True
        _REMOTE_RESUME_TIMER.start()


def _claim_remote_ids(book_ids: list[int]) -> list[int]:
    claimed: list[int] = []
    with _REMOTE_QUEUE_LOCK:
        for book_id in book_ids:
            if book_id in _REMOTE_BOOK_IDS_IN_FLIGHT:
                continue
            _REMOTE_BOOK_IDS_IN_FLIGHT.add(book_id)
            claimed.append(book_id)
    return claimed


def _release_remote_ids(book_ids: list[int]) -> None:
    with _REMOTE_QUEUE_LOCK:
        for book_id in book_ids:
            _REMOTE_BOOK_IDS_IN_FLIGHT.discard(book_id)
