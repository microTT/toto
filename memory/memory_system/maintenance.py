from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import MemoryConfig
from .state_db import StateDB


@dataclass(slots=True)
class MaintenanceResult:
    archived_paths: list[str]
    deleted_runtime_snapshots: list[str]
    deleted_job_count: int
    requeued_job_ids: list[int]

    def to_dict(self) -> dict[str, object]:
        return {
            "archived_paths": self.archived_paths,
            "deleted_runtime_snapshots": self.deleted_runtime_snapshots,
            "deleted_job_count": self.deleted_job_count,
            "requeued_job_ids": self.requeued_job_ids,
        }


def gc_runtime_snapshots(
    config: MemoryConfig,
    *,
    now: datetime | None = None,
    retention_days: int = 7,
) -> list[Path]:
    current_time = now or datetime.now(UTC)
    cutoff = current_time - timedelta(days=retention_days)
    deleted: list[Path] = []
    for path in sorted(config.runtime_dir.glob("session_*.json")):
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if modified_at < cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path)
    return deleted


def gc_finished_jobs(
    state: StateDB,
    *,
    now: datetime | None = None,
    completed_retention_days: int = 14,
    failed_retention_days: int = 30,
) -> int:
    current_time = now or datetime.now(UTC)
    completed_before = current_time - timedelta(days=completed_retention_days)
    failed_before = current_time - timedelta(days=failed_retention_days)
    return state.delete_finished_jobs(
        completed_before=completed_before,
        failed_before=failed_before,
    )
