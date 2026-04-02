from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .config import MemoryConfig
from .constants import LOCAL_ARCHIVE_SCOPE, LOCAL_RECENT_SCOPE, STATUS_ACTIVE, STATUS_OPEN
from .markdown_store import (
    MemoryDocument,
    all_records,
    empty_document,
    increment_revision,
    load_document,
    save_document,
    upsert_record,
)


def archive_stale_recent_documents(config: MemoryConfig, *, now: datetime | None = None) -> list[Path]:
    current_time = now or datetime.now(UTC)
    cutoff = current_time.date() - timedelta(days=1)
    archived_paths: list[Path] = []
    for path in sorted(config.recent_dir.glob("*.md")):
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date >= cutoff:
            continue
        document = load_document(path, LOCAL_RECENT_SCOPE)
        carry_over, archive_records = _split_recent_document(document, current_time)
        if archive_records:
            archive_path = config.archive_dir / file_date.strftime("%Y") / file_date.strftime("%m") / path.name
            archive_document = _load_or_create_archive_document(config, archive_path, file_date.isoformat())
            for record in archive_records:
                archive_document = upsert_record(archive_document, record)
            save_document(archive_path, increment_revision(archive_document))
            archived_paths.append(archive_path)
        if carry_over.sections and any(carry_over.sections.values()):
            save_document(path, increment_revision(carry_over))
        else:
            path.unlink(missing_ok=True)
    return archived_paths


def _split_recent_document(
    document: MemoryDocument, current_time: datetime
) -> tuple[MemoryDocument, list]:
    carry_over_document = empty_document(
        LOCAL_RECENT_SCOPE,
        metadata=document.metadata,
    )
    archived_records = []
    for record in all_records(document):
        keep_recent = (
            record.status in {STATUS_OPEN, STATUS_ACTIVE}
            and record.pin_until is not None
            and _parse_iso(record.pin_until) > current_time
        )
        if keep_recent:
            carry_over_document = upsert_record(carry_over_document, record)
        else:
            archived_records.append(record)
    return carry_over_document, archived_records


def _load_or_create_archive_document(
    config: MemoryConfig,
    archive_path: Path,
    file_date: str,
) -> MemoryDocument:
    if archive_path.exists():
        return load_document(archive_path, LOCAL_ARCHIVE_SCOPE)
    return empty_document(
        LOCAL_ARCHIVE_SCOPE,
        path=archive_path,
        metadata={
            "scope": LOCAL_ARCHIVE_SCOPE,
            "repo_id": config.repo_id,
            "workspace_instance_id": config.workspace_instance_id,
            "workspace_root": str(config.workspace_root),
            "date": file_date,
        },
    )


def _parse_iso(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
