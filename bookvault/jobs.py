from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import database

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_initialized = False
_last_persisted_at: dict[str, float] = {}
_last_persisted_processed: dict[str, int] = {}

PERSIST_INTERVAL_SECONDS = 10.0
PERSIST_PROGRESS_DELTA = 250
ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"finished", "failed", "interrupted"}


def create_job(kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    _ensure_loaded()
    now = time.time()
    job = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "status": "queued",
        "message": "等待开始",
        "processed": 0,
        "total": 0,
        "created_at": now,
        "updated_at": now,
        "payload": payload or {},
        "errors": [],
    }
    with _lock:
        _jobs[job["id"]] = job
        _record_persisted_locked(job, time.monotonic())
        snapshot = copy.deepcopy(job)
    database.save_background_job(snapshot)
    return snapshot


def update_job(job_id: str, **fields: Any) -> None:
    _ensure_loaded()
    snapshot: dict[str, Any] | None = None
    now = time.time()
    monotonic_now = time.monotonic()
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        previous_status = str(job.get("status") or "")
        job.update(fields)
        job["updated_at"] = now
        new_status = str(job.get("status") or "")
        force = new_status != previous_status or new_status in TERMINAL_STATUSES
        if force or _persistence_due_locked(job, monotonic_now):
            _record_persisted_locked(job, monotonic_now)
            snapshot = copy.deepcopy(job)
    if snapshot is not None:
        database.save_background_job(snapshot)


def append_error(job_id: str, message: str) -> None:
    _ensure_loaded()
    snapshot: dict[str, Any] | None = None
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        errors = list(job.get("errors") or [])
        errors.append(message[:500])
        job["errors"] = errors[-50:]
        job["updated_at"] = time.time()
        _record_persisted_locked(job, time.monotonic())
        snapshot = copy.deepcopy(job)
    database.save_background_job(snapshot)


def get_jobs() -> list[dict[str, Any]]:
    _ensure_loaded()
    with _lock:
        return [
            copy.deepcopy(job)
            for job in sorted(_jobs.values(), key=lambda item: item["created_at"], reverse=True)[:12]
        ]


def has_active(kind: str) -> bool:
    _ensure_loaded()
    with _lock:
        return any(
            job.get("kind") == kind and job.get("status") in ACTIVE_STATUSES
            for job in _jobs.values()
        )


def run_background(job_id: str, target: Callable[[str], None]) -> None:
    def runner() -> None:
        update_job(job_id, status="running", message="正在处理")
        try:
            target(job_id)
        except Exception as exc:
            append_error(job_id, str(exc))
            update_job(job_id, status="failed", message=f"任务失败：{exc}")

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()


def initialize_after_restart() -> int:
    global _initialized
    persisted_by_id = {
        str(job["id"]): job
        for job in database.load_background_jobs()
    }
    persisted_by_id.update(
        {
            str(job["id"]): job
            for job in database.load_active_background_jobs()
        }
    )
    persisted = sorted(
        persisted_by_id.values(),
        key=lambda item: float(item.get("created_at") or 0),
        reverse=True,
    )
    now = time.time()
    interrupted = 0
    snapshots: list[dict[str, Any]] = []
    with _lock:
        _jobs.clear()
        _last_persisted_at.clear()
        _last_persisted_processed.clear()
        for job in persisted:
            if str(job.get("status") or "") in ACTIVE_STATUSES:
                job["status"] = "interrupted"
                job["message"] = _interrupted_message(str(job.get("kind") or ""))
                errors = list(job.get("errors") or [])
                errors.append("服务重启时任务仍处于 queued/running，已标记为中断")
                job["errors"] = errors[-50:]
                job["interrupted_at"] = now
                job["updated_at"] = now
                snapshots.append(copy.deepcopy(job))
                interrupted += 1
            _jobs[str(job["id"])] = job
            _record_persisted_locked(job, time.monotonic())
        _initialized = True
    for snapshot in snapshots:
        database.save_background_job(snapshot)
    return interrupted


def _ensure_loaded() -> None:
    global _initialized
    with _lock:
        if _initialized:
            return
    persisted = database.load_background_jobs()
    with _lock:
        if _initialized:
            return
        for job in persisted:
            _jobs[str(job["id"])] = job
            _record_persisted_locked(job, time.monotonic())
        _initialized = True


def _persistence_due_locked(job: dict[str, Any], monotonic_now: float) -> bool:
    job_id = str(job["id"])
    last_at = _last_persisted_at.get(job_id, 0.0)
    last_processed = _last_persisted_processed.get(job_id, 0)
    processed = int(job.get("processed") or 0)
    return (
        monotonic_now - last_at >= PERSIST_INTERVAL_SECONDS
        or processed - last_processed >= PERSIST_PROGRESS_DELTA
    )


def _record_persisted_locked(job: dict[str, Any], monotonic_now: float) -> None:
    job_id = str(job["id"])
    _last_persisted_at[job_id] = monotonic_now
    _last_persisted_processed[job_id] = int(job.get("processed") or 0)


def _interrupted_message(kind: str) -> str:
    if kind == "metadata":
        return "服务重启导致任务中断；未完成元数据将由启动恢复流程重新排队"
    if kind == "scan":
        return "服务重启导致扫描中断；请确认扫描目录后重新开始"
    return "服务重启导致任务中断"


def _reset_runtime_state_for_tests() -> None:
    global _initialized
    with _lock:
        _jobs.clear()
        _last_persisted_at.clear()
        _last_persisted_processed.clear()
        _initialized = False
