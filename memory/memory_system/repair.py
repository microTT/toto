from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
from typing import Any

from .bootstrap import ensure_layout
from .config import MemoryConfig, compute_workspace_identity
from .constants import LOCAL_ARCHIVE_SCOPE, LOCAL_RECENT_SCOPE
from .markdown_store import all_records, empty_document, increment_revision, load_document, save_document, upsert_record
from .models import MemoryDocument, MemoryRecord
from .search_index import SearchIndex
from .workspace_store import build_config_for_workspace

ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?!/)[^\s`'\"<>()\[\]{}]+")
TIME_WINDOW_SECONDS = 180


def repair_mixed_workspace_store(config: MemoryConfig) -> dict[str, Any]:
    observed_configs = _observed_workspace_configs(config)
    events_conn = _open_events_connection(config)
    touched_configs: dict[Path, MemoryConfig] = {config.memory_home.resolve(): config}
    moved_records: list[dict[str, str]] = []
    kept_records: list[dict[str, str]] = []
    target_documents: dict[tuple[Path, str], MemoryDocument] = {}

    try:
        for scope, path in _iter_local_document_paths(config):
            document = load_document(path, scope)
            updated_document = empty_document(scope, path=path, metadata=dict(document.metadata))
            for section, records in document.sections.items():
                for record in records:
                    target_config, reason = _classify_record(
                        record,
                        source_document=document,
                        default_config=config,
                        observed_configs=observed_configs,
                        events_conn=events_conn,
                    )
                    if target_config.memory_home.resolve() == config.memory_home.resolve():
                        updated_document = upsert_record(updated_document, record, target_section=section)
                        kept_records.append(
                            {
                                "record_id": record.id,
                                "scope": scope,
                                "path": str(path),
                                "workspace_root": str(config.workspace_root),
                                "reason": reason,
                            }
                        )
                        continue

                    ensure_layout(target_config)
                    touched_configs[target_config.memory_home.resolve()] = target_config
                    target_path = _target_document_path(target_config, scope, path)
                    target_key = (target_path, scope)
                    if target_key not in target_documents:
                        target_documents[target_key] = _load_or_create_target_document(
                            target_config,
                            scope=scope,
                            path=target_path,
                            file_date=str(document.metadata.get("date") or _document_date_from_path(path)),
                        )
                    target_documents[target_key] = upsert_record(
                        target_documents[target_key],
                        record,
                        target_section=section,
                    )
                    moved_records.append(
                        {
                            "record_id": record.id,
                            "scope": scope,
                            "from_path": str(path),
                            "to_path": str(target_path),
                            "workspace_root": str(target_config.workspace_root),
                            "reason": reason,
                        }
                    )

            _save_or_delete_document(path, original=document, updated=updated_document)

        for (target_path, _scope), document in target_documents.items():
            existing = load_document(target_path, document.scope) if target_path.exists() else None
            if existing is not None and existing == document:
                continue
            save_document(target_path, increment_revision(document))

        for touched_config in touched_configs.values():
            _refresh_index(touched_config)
    finally:
        if events_conn is not None:
            events_conn.close()

    return {
        "memory_home": str(config.memory_home),
        "moved_records": moved_records,
        "kept_records": kept_records,
        "target_memory_homes": [str(path) for path in sorted(touched_configs)],
    }


def _observed_workspace_configs(config: MemoryConfig) -> dict[str, MemoryConfig]:
    observed: dict[str, MemoryConfig] = {config.workspace_instance_id: config}
    candidates: set[Path] = {config.workspace_root}
    state_db_path = config.state_db_path
    if state_db_path.exists():
        conn = sqlite3.connect(str(state_db_path))
        try:
            conn.row_factory = sqlite3.Row
            for sql in (
                "SELECT DISTINCT workspace_root FROM summary_jobs",
                "SELECT DISTINCT workspace_root FROM session_snapshots",
                "SELECT DISTINCT cwd AS workspace_root FROM events WHERE cwd IS NOT NULL AND cwd != ''",
            ):
                for row in conn.execute(sql).fetchall():
                    value = row["workspace_root"]
                    if value:
                        candidates.add(Path(str(value)).expanduser().resolve())
        finally:
            conn.close()

    for scope, path in _iter_local_document_paths(config):
        document = load_document(path, scope)
        workspace_root = document.metadata.get("workspace_root")
        if workspace_root:
            candidates.add(Path(str(workspace_root)).expanduser().resolve())
        for record in all_records(document):
            for probe in record.source_refs:
                candidate_root = _workspace_root_from_probe(probe)
                if candidate_root is not None:
                    candidates.add(candidate_root)

    parent = config.memory_home.resolve().parent
    for workspace_root in sorted(candidates):
        identity = compute_workspace_identity(workspace_root)
        home = parent / identity.workspace_instance_id
        observed[identity.workspace_instance_id] = build_config_for_workspace(workspace_root, home)
    return observed


def _classify_record(
    record: MemoryRecord,
    *,
    source_document: MemoryDocument,
    default_config: MemoryConfig,
    observed_configs: dict[str, MemoryConfig],
    events_conn: sqlite3.Connection | None,
) -> tuple[MemoryConfig, str]:
    source_ref_matches = _workspace_matches_for_paths(record.source_refs, observed_configs)
    if len(source_ref_matches) == 1:
        target = next(iter(source_ref_matches.values()))
        return target, "source_refs"

    text_paths = _text_path_probes(record)
    if events_conn is not None:
        event_matches = _workspace_matches_from_events(
            events_conn,
            probes=[*record.source_refs, *text_paths],
            record_time=_record_time(record),
            observed_configs=observed_configs,
        )
        if len(event_matches) == 1:
            target = next(iter(event_matches.values()))
            return target, "events"

    document_root = source_document.metadata.get("workspace_root")
    if document_root:
        document_identity = compute_workspace_identity(Path(str(document_root)).expanduser().resolve())
        target = observed_configs.get(document_identity.workspace_instance_id)
        if target is not None:
            return target, "document_metadata"
    return default_config, "default_current_workspace"


def _workspace_matches_for_paths(
    probes: list[str],
    observed_configs: dict[str, MemoryConfig],
) -> dict[str, MemoryConfig]:
    matches: dict[str, MemoryConfig] = {}
    for raw_probe in probes:
        probe = _normalize_path_probe(raw_probe)
        if not probe:
            continue
        probe_path = Path(probe).expanduser()
        if not probe_path.is_absolute():
            continue
        resolved_probe = probe_path.resolve()
        for workspace_instance_id, candidate in observed_configs.items():
            try:
                resolved_probe.relative_to(candidate.workspace_root)
            except ValueError:
                continue
            matches[workspace_instance_id] = candidate
    return matches


def _workspace_matches_from_events(
    conn: sqlite3.Connection,
    *,
    probes: list[str],
    record_time: datetime,
    observed_configs: dict[str, MemoryConfig],
) -> dict[str, MemoryConfig]:
    if not probes:
        return {}
    matches: dict[str, MemoryConfig] = {}
    window_start = _isoformat(record_time - timedelta(seconds=TIME_WINDOW_SECONDS))
    window_end = _isoformat(record_time + timedelta(seconds=TIME_WINDOW_SECONDS))
    conn.row_factory = sqlite3.Row
    for probe in probes:
        normalized_probe = _normalize_path_probe(probe)
        if not normalized_probe:
            continue
        escaped = normalized_probe.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            """
            SELECT DISTINCT cwd
            FROM events
            WHERE event_time BETWEEN ? AND ?
              AND (
                user_message_delta LIKE ? ESCAPE '\\'
                OR assistant_message_delta LIKE ? ESCAPE '\\'
              )
              AND cwd IS NOT NULL
              AND cwd != ''
            """,
            (window_start, window_end, f"%{escaped}%", f"%{escaped}%"),
        ).fetchall()
        for row in rows:
            identity = compute_workspace_identity(Path(str(row["cwd"])).expanduser().resolve())
            candidate = observed_configs.get(identity.workspace_instance_id)
            if candidate is not None:
                matches[candidate.workspace_instance_id] = candidate
    return matches


def _iter_local_document_paths(config: MemoryConfig) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    for path in sorted(config.recent_dir.glob("*.md")):
        paths.append((LOCAL_RECENT_SCOPE, path))
    for path in sorted(config.archive_dir.glob("*/*/*.md")):
        paths.append((LOCAL_ARCHIVE_SCOPE, path))
    return paths


def _load_or_create_target_document(
    config: MemoryConfig,
    *,
    scope: str,
    path: Path,
    file_date: str,
) -> MemoryDocument:
    if path.exists():
        return load_document(path, scope)
    metadata = {
        "repo_id": config.repo_id,
        "workspace_instance_id": config.workspace_instance_id,
        "workspace_root": str(config.workspace_root),
        "date": file_date,
    }
    return empty_document(scope, path=path, metadata=metadata)


def _target_document_path(config: MemoryConfig, scope: str, source_path: Path) -> Path:
    if scope == LOCAL_RECENT_SCOPE:
        return config.recent_dir / source_path.name
    return config.archive_dir / source_path.parent.parent.name / source_path.parent.name / source_path.name


def _document_date_from_path(path: Path) -> str:
    if path.parent.parent.name.isdigit() and path.parent.name.isdigit():
        return f"{path.parent.parent.name}-{path.parent.name}-01"
    return path.stem


def _save_or_delete_document(path: Path, *, original: MemoryDocument, updated: MemoryDocument) -> None:
    if updated == original:
        return
    if any(updated.sections.values()):
        save_document(path, increment_revision(updated))
        return
    path.unlink(missing_ok=True)


def _record_time(record: MemoryRecord) -> datetime:
    raw = record.updated_at or record.created_at
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(UTC)


def _text_path_probes(record: MemoryRecord) -> list[str]:
    values = [record.subject, record.summary]
    if record.rationale:
        values.append(record.rationale)
    if record.next_use:
        values.append(record.next_use)
    probes: list[str] = []
    for value in values:
        probes.extend(_extract_absolute_paths(value))
    return probes


def _extract_absolute_paths(text: str) -> list[str]:
    return [_normalize_path_probe(match.group(0)) for match in ABSOLUTE_PATH_RE.finditer(text)]


def _normalize_path_probe(raw: str) -> str:
    return raw.strip().rstrip("`'\"),.:;!?。，）】]")


def _workspace_root_from_probe(raw: str) -> Path | None:
    probe = _normalize_path_probe(raw)
    if not probe:
        return None
    probe_path = Path(probe).expanduser()
    if not probe_path.is_absolute():
        return None
    anchor = probe_path if probe_path.exists() and probe_path.is_dir() else probe_path.parent
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    resolved_anchor = anchor.resolve()
    home = Path.home().resolve()
    if resolved_anchor == Path(resolved_anchor.anchor):
        return None
    if resolved_anchor == home:
        return None
    if any(part.startswith(".") for part in resolved_anchor.parts[1:]):
        return None
    return compute_workspace_identity(resolved_anchor).workspace_root


def _open_events_connection(config: MemoryConfig) -> sqlite3.Connection | None:
    if not config.state_db_path.exists():
        return None
    return sqlite3.connect(str(config.state_db_path))


def _refresh_index(config: MemoryConfig) -> None:
    index = SearchIndex(config.index_db_path)
    try:
        index.rebuild(config)
    finally:
        index.close()


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
