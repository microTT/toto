from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    DEFAULT_SCHEMA_VERSION,
    GLOBAL_SCOPE,
    GLOBAL_SECTION_ORDER,
    LOCAL_RECENT_SCOPE,
    LOCAL_SECTION_ORDER,
    STATUS_TO_SECTION,
)
from .models import MemoryDocument, MemoryRecord
from .utils import atomic_write_text, isoformat


class MarkdownStoreError(RuntimeError):
    pass


def load_document(path: Path, scope: str) -> MemoryDocument:
    if not path.exists():
        return empty_document(scope, path=path)
    text = path.read_text(encoding="utf-8")
    return parse_document(text, scope)


def empty_document(scope: str, *, path: Path | None = None, metadata: dict[str, Any] | None = None) -> MemoryDocument:
    base_metadata = {
        "schema_version": DEFAULT_SCHEMA_VERSION,
        "scope": scope,
        "revision": 0,
        "updated_at": isoformat(),
    }
    if scope == LOCAL_RECENT_SCOPE:
        base_metadata.setdefault("date", _date_from_path(path))
    if metadata:
        base_metadata.update(metadata)
    section_order = GLOBAL_SECTION_ORDER if scope == GLOBAL_SCOPE else LOCAL_SECTION_ORDER
    return MemoryDocument(scope=scope, metadata=base_metadata, sections={section: [] for section in section_order})


def parse_document(text: str, scope: str) -> MemoryDocument:
    lines = text.splitlines()
    metadata, index = _parse_frontmatter(lines)
    section_order = GLOBAL_SECTION_ORDER if scope == GLOBAL_SCOPE else LOCAL_SECTION_ORDER
    sections: dict[str, list[MemoryRecord]] = {section: [] for section in section_order}
    current_section: str | None = None
    current_record_id: str | None = None
    current_fields: dict[str, Any] = {}

    def flush_record() -> None:
        nonlocal current_record_id, current_fields
        if current_record_id is None or current_section is None:
            return
        payload = dict(current_fields)
        payload["id"] = current_record_id
        payload.setdefault("status", _section_to_status(current_section))
        payload.setdefault("confidence", "medium")
        payload.setdefault("tags", [])
        payload.setdefault("source_refs", [])
        payload.setdefault("supersedes", [])
        sections[current_section].append(MemoryRecord.from_dict(payload))
        current_record_id = None
        current_fields = {}

    for line in lines[index:]:
        if line.startswith("## "):
            flush_record()
            heading = line[3:].strip()
            current_section = heading
            if heading not in sections:
                sections[heading] = []
            continue
        if line.startswith("### "):
            flush_record()
            current_record_id = line[4:].strip()
            continue
        if current_record_id and line.startswith("- "):
            key, value = _parse_bullet_field(line[2:])
            current_fields[key] = value
    flush_record()
    return MemoryDocument(scope=scope, metadata=metadata, sections=sections)


def render_document(document: MemoryDocument) -> str:
    section_order = GLOBAL_SECTION_ORDER if document.scope == GLOBAL_SCOPE else LOCAL_SECTION_ORDER
    metadata = dict(document.metadata)
    lines = ["---"]
    for key in sorted(metadata):
        value = metadata[key]
        lines.append(f"{key}: {_render_scalar(value)}")
    lines.append("---")
    lines.append("")
    if document.scope == GLOBAL_SCOPE:
        lines.append("# Global Long-Term Memory")
    else:
        date_label = metadata.get("date", "")
        lines.append(f"# Local Memory - {date_label}")
    lines.append("")
    for section in section_order:
        records = document.sections.get(section, [])
        lines.append(f"## {section}")
        lines.append("")
        for record in records:
            lines.extend(_render_record(record))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_document(path: Path, document: MemoryDocument) -> None:
    atomic_write_text(path, render_document(document))


def increment_revision(document: MemoryDocument) -> MemoryDocument:
    metadata = dict(document.metadata)
    metadata["revision"] = int(metadata.get("revision", 0)) + 1
    metadata["updated_at"] = isoformat()
    return MemoryDocument(scope=document.scope, metadata=metadata, sections=document.sections)


def upsert_record(document: MemoryDocument, record: MemoryRecord, target_section: str | None = None) -> MemoryDocument:
    sections = {name: list(records) for name, records in document.sections.items()}
    record = replace(record, updated_at=record.updated_at or isoformat())
    for records in sections.values():
        records[:] = [item for item in records if item.id != record.id]
    section = target_section or STATUS_TO_SECTION[record.status]
    sections.setdefault(section, []).append(record)
    sections[section].sort(key=lambda item: (item.updated_at, item.id))
    return MemoryDocument(scope=document.scope, metadata=dict(document.metadata), sections=sections)


def get_record(document: MemoryDocument, record_id: str) -> tuple[str, MemoryRecord] | None:
    for section, records in document.sections.items():
        for record in records:
            if record.id == record_id:
                return section, record
    return None


def all_records(document: MemoryDocument) -> Iterable[MemoryRecord]:
    for records in document.sections.values():
        yield from records


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, Any], int]:
    if not lines or lines[0].strip() != "---":
        raise MarkdownStoreError("memory document is missing frontmatter")
    metadata: dict[str, Any] = {}
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        idx += 1
        if line.strip() == "---":
            break
        if not line.strip():
            continue
        key, value = _parse_bullet_field(line)
        metadata[key] = value
    return metadata, idx


def _parse_bullet_field(text: str) -> tuple[str, Any]:
    if ":" not in text:
        raise MarkdownStoreError(f"invalid field line: {text}")
    key, raw_value = text.split(":", 1)
    return key.strip(), _parse_scalar(raw_value.strip())


def _parse_scalar(value: str) -> Any:
    if value == "":
        return ""
    if value == "null":
        return None
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        if '"' in inner or "'" in inner:
            return list(ast.literal_eval(value))
        return [part.strip() for part in inner.split(",") if part.strip()]
    if value.isdigit():
        return int(value)
    return value


def _render_record(record: MemoryRecord) -> list[str]:
    lines = [f"### {record.id}"]
    payload = record.to_dict()
    ordered_keys = [
        "type",
        "status",
        "confidence",
        "subject",
        "summary",
        "rationale",
        "next_use",
        "tags",
        "source_refs",
        "created_at",
        "updated_at",
        "pin_until",
        "supersedes",
        "superseded_by",
        "scope_reason",
    ]
    for key in ordered_keys:
        value = payload.get(key)
        if value in (None, "", []):
            continue
        lines.append(f"- {key}: {_render_scalar(value)}")
    return lines


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(f'"{item}"' for item in value) + "]"
    return str(value)


def _section_to_status(section: str) -> str:
    for status, heading in STATUS_TO_SECTION.items():
        if heading == section:
            return status
    raise MarkdownStoreError(f"unknown section: {section}")


def _date_from_path(path: Path | None) -> str:
    if path is not None and path.stem:
        try:
            parsed = datetime.strptime(path.stem, "%Y-%m-%d")
            return parsed.replace(tzinfo=UTC).date().isoformat()
        except ValueError:
            pass
    return isoformat()[:10]
