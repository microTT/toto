from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .archive import archive_stale_recent_documents
from .bootstrap import ensure_layout
from .config import resolve_config
from .constants import (
    DEFAULT_COMPLETED_JOB_RETENTION_DAYS,
    DEFAULT_FAILED_JOB_RETENTION_DAYS,
    DEFAULT_JOB_RETRY_BASE_SECONDS,
    DEFAULT_RUNNING_JOB_TIMEOUT_SECONDS,
)
from .errors import PatchApplyError, SummarizerExecutionError
from .maintenance import gc_finished_jobs, gc_runtime_snapshots
from .patch_applier import apply_patch_plan
from .search_index import SearchIndex
from .state_db import StateDB
from .summarizer import summarize_job
from .utils import isoformat


def run_worker_once(
    cwd: str | None = None,
    *,
    memory_home: str | None = None,
    backend: str = "codex",
    retry_base_seconds: int = DEFAULT_JOB_RETRY_BASE_SECONDS,
    running_job_timeout_seconds: int = DEFAULT_RUNNING_JOB_TIMEOUT_SECONDS,
    completed_job_retention_days: int = DEFAULT_COMPLETED_JOB_RETENTION_DAYS,
    failed_job_retention_days: int = DEFAULT_FAILED_JOB_RETENTION_DAYS,
) -> dict[str, Any] | None:
    config = resolve_config(cwd, memory_home)
    ensure_layout(config)
    current_time = datetime.now(UTC)
    archived = archive_stale_recent_documents(config, now=current_time)
    deleted_runtime_snapshots = gc_runtime_snapshots(config, now=current_time)
    state = StateDB(config.state_db_path)
    try:
        requeued_job_ids = state.requeue_stale_running_jobs(
            stale_before=current_time - timedelta(seconds=running_job_timeout_seconds),
            retry_at=current_time,
        )
        deleted_job_count = gc_finished_jobs(
            state,
            now=current_time,
            completed_retention_days=completed_job_retention_days,
            failed_retention_days=failed_job_retention_days,
        )
        job = state.fetch_next_pending_job(now=current_time)
        if job is None:
            if archived or deleted_runtime_snapshots or deleted_job_count or requeued_job_ids:
                _refresh_index(config)
                return {
                    "job": None,
                    "archived": [str(path) for path in archived],
                    "deleted_runtime_snapshots": [str(path) for path in deleted_runtime_snapshots],
                    "deleted_job_count": deleted_job_count,
                    "requeued_job_ids": requeued_job_ids,
                    "applied": False,
                }
            return None
        state.mark_job_running(job.id)
        events = [
            dict(row)
            for row in state.get_events_range(job.session_id, (job.start_event_id or 0), job.end_event_id)
        ]
        patch_plan = summarize_job(config=config, job=job, events=events, backend=backend)
        if patch_plan.get("decision") == "noop":
            state.update_job_status(job.id, "completed", {"result": patch_plan})
            if job.end_event_id is not None:
                state.update_summary_cursor(job.session_id, job.end_event_id, None)
            _refresh_index(config)
            return {
                "job": asdict(job),
                "patch_plan": patch_plan,
                "archived": [str(path) for path in archived],
                "deleted_runtime_snapshots": [str(path) for path in deleted_runtime_snapshots],
                "deleted_job_count": deleted_job_count,
                "requeued_job_ids": requeued_job_ids,
                "applied": False,
            }
        apply_result = apply_patch_plan(config, patch_plan)
        _refresh_index(config)
        state.update_job_status(job.id, "completed", {"result": patch_plan, "apply_result": apply_result})
        if job.end_event_id is not None:
            state.update_summary_cursor(job.session_id, job.end_event_id, None)
        return {
            "job": asdict(job),
            "patch_plan": patch_plan,
            "apply_result": apply_result,
            "archived": [str(path) for path in archived],
            "deleted_runtime_snapshots": [str(path) for path in deleted_runtime_snapshots],
            "deleted_job_count": deleted_job_count,
            "requeued_job_ids": requeued_job_ids,
            "applied": True,
        }
    except PatchApplyError as exc:
        if "job" in locals():
            state.mark_job_failed(job.id, error=str(exc), payload={"error": str(exc), "kind": "patch_apply"})
            return {
                "job": asdict(job),
                "error": str(exc),
                "error_kind": "patch_apply",
                "archived": [str(path) for path in archived],
                "deleted_runtime_snapshots": [str(path) for path in deleted_runtime_snapshots],
                "deleted_job_count": deleted_job_count,
                "requeued_job_ids": requeued_job_ids,
                "applied": False,
            }
        raise
    except SummarizerExecutionError as exc:
        if "job" in locals():
            retry_status = _retry_or_fail_job(
                state,
                job,
                error=str(exc),
                retry_base_seconds=retry_base_seconds,
                current_time=current_time,
                payload={"error": str(exc), "kind": "summarizer"},
            )
            return {
                "job": asdict(job),
                "error": str(exc),
                "error_kind": "summarizer",
                "retry_status": retry_status,
                "archived": [str(path) for path in archived],
                "deleted_runtime_snapshots": [str(path) for path in deleted_runtime_snapshots],
                "deleted_job_count": deleted_job_count,
                "requeued_job_ids": requeued_job_ids,
                "applied": False,
            }
        raise
    except Exception as exc:
        if "job" in locals():
            retry_status = _retry_or_fail_job(
                state,
                job,
                error=str(exc),
                retry_base_seconds=retry_base_seconds,
                current_time=current_time,
                payload={"error": str(exc), "kind": "unexpected"},
            )
            return {
                "job": asdict(job),
                "error": str(exc),
                "error_kind": "unexpected",
                "retry_status": retry_status,
                "archived": [str(path) for path in archived],
                "deleted_runtime_snapshots": [str(path) for path in deleted_runtime_snapshots],
                "deleted_job_count": deleted_job_count,
                "requeued_job_ids": requeued_job_ids,
                "applied": False,
            }
        raise
    finally:
        state.close()


def _refresh_index(config) -> None:
    index = SearchIndex(config.index_db_path)
    try:
        index.rebuild(config)
    finally:
        index.close()


def _retry_or_fail_job(
    state: StateDB,
    job,
    *,
    error: str,
    retry_base_seconds: int,
    current_time: datetime,
    payload: dict[str, Any],
) -> dict[str, Any]:
    attempt_count = job.attempt_count + 1
    if attempt_count >= job.max_attempts:
        state.mark_job_failed(job.id, error=error, payload=payload)
        return {"status": "failed", "attempt_count": attempt_count, "max_attempts": job.max_attempts}
    backoff_seconds = retry_base_seconds * (2 ** max(0, attempt_count - 1))
    updated_job = state.schedule_job_retry(
        job.id,
        error=error,
        next_attempt_at=current_time + timedelta(seconds=backoff_seconds),
    )
    return {
        "status": "retry_wait",
        "attempt_count": updated_job.attempt_count,
        "max_attempts": updated_job.max_attempts,
        "next_attempt_at": updated_job.next_attempt_at,
    }
