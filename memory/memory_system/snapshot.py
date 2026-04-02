from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .config import MemoryConfig
from .constants import (
    DEFAULT_GLOBAL_LIMIT,
    DEFAULT_LOCAL_LIMIT,
    GLOBAL_SCOPE,
    LOCAL_RECENT_SCOPE,
    STATUS_ACTIVE,
    STATUS_OPEN,
)
from .markdown_store import all_records, empty_document, load_document
from .models import MemoryRecord, Snapshot
from .utils import estimate_tokens, isoformat, sha256_text


def build_snapshot(config: MemoryConfig, *, now: datetime | None = None) -> Snapshot:
    current_time = now or datetime.now(UTC)
    global_document = load_document(config.global_memory_path, GLOBAL_SCOPE)
    recent_files = sorted(config.recent_dir.glob("*.md"))
    recent_documents = [load_document(path, LOCAL_RECENT_SCOPE) for path in recent_files]

    global_records = [record for record in global_document.sections.get("Active", []) if record.status == STATUS_ACTIVE]
    local_records = _select_local_recent_records(recent_documents, current_time)

    selected_global = _sort_global_records(global_records)[:DEFAULT_GLOBAL_LIMIT]
    selected_local = _apply_budget(
        global_records=selected_global,
        local_records=_sort_local_records(local_records),
        token_budget=config.token_budget,
    )[:DEFAULT_LOCAL_LIMIT]

    rendered_text = render_snapshot_block(selected_global, selected_local)
    fingerprint = compute_source_fingerprint(config, recent_files)
    revision = sha256_text(
        fingerprint
        + "|"
        + "|".join(record.id for record in selected_global)
        + "|"
        + "|".join(record.id for record in selected_local)
        + str(config.token_budget)
    )[:16]
    return Snapshot(
        revision=revision,
        global_records=selected_global,
        local_records=selected_local,
        rendered_text=rendered_text,
        source_fingerprint=fingerprint,
        built_at=isoformat(current_time),
    )


def compute_source_fingerprint(config: MemoryConfig, recent_files: list[Path] | None = None) -> str:
    files = [config.global_memory_path]
    if recent_files is None:
        recent_files = sorted(config.recent_dir.glob("*.md"))
    files.extend(recent_files)
    parts: list[str] = [config.repo_id, config.workspace_instance_id, str(config.token_budget)]
    for path in files:
        if not path.exists():
            parts.append(f"{path}:missing")
            continue
        stat = path.stat()
        parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
    return sha256_text("|".join(parts))


def render_snapshot_block(global_records: list[MemoryRecord], local_records: list[MemoryRecord]) -> str:
    lines = ["[MEMORY LOADED]", "", "Global long-term memory:"]
    if global_records:
        for record in global_records:
            lines.append(f"- {_render_record_line(record)}")
    else:
        lines.append("- None.")
    lines.extend(["", "Workspace recent memory:"])
    if local_records:
        for record in local_records:
            lines.append(f"- {_render_record_line(record)}")
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "Older workspace memory is NOT auto-loaded.",
            "If the user refers to earlier attempts, previous bugs, past decisions, or what we did before,",
            "use MCP tool: memory.search_old",
        ]
    )
    return "\n".join(lines)


def _render_record_line(record: MemoryRecord) -> str:
    segments = [f"[{record.type}] {record.subject}: {record.summary}"]
    if record.next_use:
        segments.append(f"next_use={record.next_use}")
    if record.tags:
        segments.append(f"tags={','.join(record.tags)}")
    if record.pin_until:
        segments.append(f"pin_until={record.pin_until}")
    return " | ".join(segments)


def _select_local_recent_records(documents: Iterable, now: datetime) -> list[MemoryRecord]:
    today = now.date()
    yesterday = (now - timedelta(days=1)).date()
    selected: dict[str, MemoryRecord] = {}
    for document in documents:
        file_date = _document_date(document)
        for section_name in ("Open", "Active"):
            for record in document.sections.get(section_name, []):
                if record.status not in {STATUS_OPEN, STATUS_ACTIVE}:
                    continue
                include = file_date in {today, yesterday}
                if not include and record.pin_until:
                    include = _parse_iso(record.pin_until) > now
                if not include:
                    continue
                previous = selected.get(record.id)
                if previous is None or (record.updated_at, record.id) > (previous.updated_at, previous.id):
                    selected[record.id] = replace(record)
    return list(selected.values())


def _sort_global_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    return sorted(
        records,
        key=lambda record: (
            confidence_rank.get(record.confidence, 0),
            record.updated_at,
            record.id,
        ),
        reverse=True,
    )


def _sort_local_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    def key(record: MemoryRecord) -> tuple[int, str, str]:
        if record.status == STATUS_OPEN:
            pin_value = record.pin_until or ""
            return (0, pin_value, record.updated_at)
        return (1, record.updated_at, record.id)

    ordered = sorted(records, key=key, reverse=True)
    open_records = [record for record in ordered if record.status == STATUS_OPEN]
    active_records = [record for record in ordered if record.status == STATUS_ACTIVE]
    return open_records + active_records


def _apply_budget(
    *,
    global_records: list[MemoryRecord],
    local_records: list[MemoryRecord],
    token_budget: int,
) -> list[MemoryRecord]:
    used_tokens = estimate_tokens(render_snapshot_block(global_records, []))
    selected: list[MemoryRecord] = []
    for record in local_records:
        record_text = f"- {_render_record_line(record)}"
        if selected and used_tokens + estimate_tokens(record_text) > token_budget:
            break
        selected.append(record)
        used_tokens += estimate_tokens(record_text)
    return selected


def _document_date(document) -> datetime.date:
    raw = document.metadata.get("date")
    if not raw:
        return datetime.now(UTC).date()
    return datetime.strptime(str(raw), "%Y-%m-%d").date()


def _parse_iso(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
