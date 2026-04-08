from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audit import append_audit_event
from .config import MemoryConfig
from .constants import GLOBAL_SCOPE, LOCAL_RECENT_SCOPE, STATUS_DELETED, STATUS_SUPERSEDED
from .errors import PatchApplyError
from .file_lock import exclusive_lock
from .markdown_store import (
    empty_document,
    get_record,
    increment_revision,
    load_document,
    save_document,
    upsert_record,
)
from .models import MemoryDocument, MemoryRecord
from .utils import isoformat, sha256_text
from .validation import reject_secrets_in_patch_plan, validate_patch_plan
from .workspace_store import iter_scoped_recent_documents


def apply_patch_plan(config: MemoryConfig, patch_plan: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    current_time = now or datetime.now(UTC)
    validate_patch_plan(patch_plan)
    reject_secrets_in_patch_plan(patch_plan)
    base_revisions = patch_plan.get("base_revisions", {})
    global_path = config.global_memory_path

    with exclusive_lock(config.lock_path("global")), exclusive_lock(
        config.lock_path(config.workspace_instance_id)
    ):
        global_document = load_document(global_path, GLOBAL_SCOPE)
        local_documents = _load_recent_documents(config, current_time)

        _validate_base_revisions(global_document, local_documents, base_revisions)

        updated_global, updated_local_documents = _apply_ops_to_global(
            config,
            global_document,
            local_documents,
            patch_plan.get("global_ops", []),
            current_time,
        )
        updated_global, updated_local_documents = _apply_ops_to_local(
            config,
            updated_global,
            updated_local_documents,
            patch_plan.get("local_ops", []),
            current_time,
        )

        if updated_global != global_document:
            updated_global = increment_revision(updated_global)
            save_document(global_path, updated_global)
        updated_local_revision = _recent_documents_revision(local_documents)
        for path, updated_document in updated_local_documents.items():
            if updated_document != local_documents[path]:
                updated_document = increment_revision(updated_document)
                updated_local_documents[path] = updated_document
                save_document(path, updated_document)
        updated_local_revision = _recent_documents_revision(updated_local_documents)

    audit_payload = {
        "applied_at": isoformat(current_time),
        "decision": patch_plan.get("decision", "write"),
        "reason": patch_plan.get("reason", ""),
        "global_ops": patch_plan.get("global_ops", []),
        "local_ops": patch_plan.get("local_ops", []),
        "workspace_instance_id": config.workspace_instance_id,
        "repo_id": config.repo_id,
    }
    append_audit_event(config.global_audit_path, audit_payload)
    append_audit_event(config.workspace_audit_path, audit_payload)
    return {
        "global_revision": updated_global.revision,
        "local_recent_revision": updated_local_revision,
        "audit_id": sha256_text(str(audit_payload))[:12],
    }


def current_base_revisions(config: MemoryConfig, *, now: datetime | None = None) -> dict[str, int]:
    current_time = now or datetime.now(UTC)
    global_document = load_document(config.global_memory_path, GLOBAL_SCOPE)
    local_documents = _load_recent_documents(config, current_time)
    return {
        "global_revision": global_document.revision,
        "local_recent_revision": _recent_documents_revision(local_documents),
    }


def _validate_base_revisions(
    global_document: MemoryDocument,
    local_documents: dict[Path, MemoryDocument],
    base_revisions: dict[str, Any],
) -> None:
    if not base_revisions:
        raise PatchApplyError("patch plan is missing base_revisions")
    expected_global = int(base_revisions.get("global_revision", -1))
    expected_local = int(base_revisions.get("local_recent_revision", -1))
    if global_document.revision != expected_global:
        raise PatchApplyError(
            f"global revision mismatch: expected {expected_global}, got {global_document.revision}"
        )
    current_local_revision = _recent_documents_revision(local_documents)
    if current_local_revision != expected_local:
        raise PatchApplyError(
            f"local revision mismatch: expected {expected_local}, got {current_local_revision}"
        )


def _load_recent_documents(config: MemoryConfig, current_time: datetime) -> dict[Path, MemoryDocument]:
    documents: dict[Path, MemoryDocument] = {}
    for path, document in iter_scoped_recent_documents(config):
        documents[path] = document
    today_path = config.recent_dir / f"{current_time.date().isoformat()}.md"
    if today_path not in documents:
        documents[today_path] = empty_document(
            LOCAL_RECENT_SCOPE,
            path=today_path,
            metadata={
                "repo_id": config.repo_id,
                "workspace_instance_id": config.workspace_instance_id,
                "workspace_root": str(config.workspace_root),
                "date": current_time.date().isoformat(),
            },
        )
    return documents


def _apply_ops_to_global(
    config: MemoryConfig,
    document: MemoryDocument,
    local_documents: dict[Path, MemoryDocument],
    ops: list[dict[str, Any]],
    current_time: datetime,
) -> tuple[MemoryDocument, dict[Path, MemoryDocument]]:
    updated = copy.deepcopy(document)
    updated_local_documents = copy.deepcopy(local_documents)
    today_path = config.recent_dir / f"{current_time.date().isoformat()}.md"
    for op in ops:
        action = op["action"]
        if action == "create":
            updated = upsert_record(updated, _record_from_payload(op["record"], current_time, prefix="g_"))
            continue
        target = _require_record(updated, op["target_id"])
        if action == "update":
            patched = _patch_record(target[1], op["record_patch"], current_time)
            updated = upsert_record(updated, patched)
        elif action == "supersede":
            old_record = replace(target[1], status=STATUS_SUPERSEDED, superseded_by=None, updated_at=isoformat(current_time))
            updated = upsert_record(updated, old_record)
            replacement = _record_from_payload(
                op["replacement_record"], current_time, prefix="g_", supersedes=[target[1].id]
            )
            updated = upsert_record(updated, replacement)
            old_record = replace(old_record, superseded_by=replacement.id)
            updated = upsert_record(updated, old_record)
        elif action == "delete":
            reason = op.get("tombstone", {}).get("reason")
            if not reason:
                raise PatchApplyError("delete op requires tombstone.reason")
            deleted = replace(
                target[1],
                status=STATUS_DELETED,
                rationale=reason,
                updated_at=isoformat(current_time),
            )
            updated = upsert_record(updated, deleted)
        elif action == "demote":
            old_record = replace(
                target[1],
                status=STATUS_SUPERSEDED,
                superseded_by=None,
                updated_at=isoformat(current_time),
            )
            updated = upsert_record(updated, old_record)
            replacement = _record_from_payload(
                op["replacement_record"],
                current_time,
                prefix="l_",
                supersedes=[target[1].id],
            )
            updated_local_documents[today_path] = upsert_record(updated_local_documents[today_path], replacement)
            old_record = replace(old_record, superseded_by=replacement.id)
            updated = upsert_record(updated, old_record)
        else:
            raise PatchApplyError(f"unsupported global op: {action}")
    return updated, updated_local_documents


def _apply_ops_to_local(
    config: MemoryConfig,
    global_document: MemoryDocument,
    documents: dict[Path, MemoryDocument],
    ops: list[dict[str, Any]],
    current_time: datetime,
) -> tuple[MemoryDocument, dict[Path, MemoryDocument]]:
    updated_global = copy.deepcopy(global_document)
    updated = copy.deepcopy(documents)
    today_path = config.recent_dir / f"{current_time.date().isoformat()}.md"
    for op in ops:
        action = op["action"]
        if action == "create":
            record = _record_from_payload(op["record"], current_time, prefix="l_")
            updated[today_path] = upsert_record(updated[today_path], record)
            continue
        target_path, target = _require_record_in_documents(updated, op["target_id"])
        if action == "update":
            patched = _patch_record(target, op["record_patch"], current_time)
            updated[target_path] = upsert_record(updated[target_path], patched)
        elif action == "supersede":
            old_record = replace(target, status=STATUS_SUPERSEDED, updated_at=isoformat(current_time))
            updated[target_path] = upsert_record(updated[target_path], old_record)
            replacement = _record_from_payload(
                op["replacement_record"], current_time, prefix="l_", supersedes=[target.id]
            )
            updated[today_path] = upsert_record(updated[today_path], replacement)
            old_record = replace(old_record, superseded_by=replacement.id)
            updated[target_path] = upsert_record(updated[target_path], old_record)
        elif action == "delete":
            reason = op.get("tombstone", {}).get("reason")
            if not reason:
                raise PatchApplyError("delete op requires tombstone.reason")
            deleted = replace(
                target,
                status=STATUS_DELETED,
                rationale=reason,
                updated_at=isoformat(current_time),
            )
            updated[target_path] = upsert_record(updated[target_path], deleted)
        elif action == "pin":
            pin_until = op.get("pin", {}).get("pin_until")
            if not pin_until:
                raise PatchApplyError("pin op requires pin.pin_until")
            pinned = replace(target, pin_until=pin_until, updated_at=isoformat(current_time))
            updated[target_path] = upsert_record(updated[target_path], pinned)
        elif action == "promote":
            old_record = replace(
                target,
                status=STATUS_SUPERSEDED,
                superseded_by=None,
                updated_at=isoformat(current_time),
            )
            updated[target_path] = upsert_record(updated[target_path], old_record)
            replacement = _record_from_payload(
                op["replacement_record"],
                current_time,
                prefix="g_",
                supersedes=[target.id],
            )
            updated_global = upsert_record(updated_global, replacement)
            old_record = replace(old_record, superseded_by=replacement.id)
            updated[target_path] = upsert_record(updated[target_path], old_record)
        else:
            raise PatchApplyError(f"unsupported local op: {action}")
    return updated_global, updated


def _record_from_payload(
    payload: dict[str, Any],
    current_time: datetime,
    *,
    prefix: str,
    supersedes: list[str] | None = None,
) -> MemoryRecord:
    timestamp = isoformat(current_time)
    record_payload = dict(payload)
    record_payload.setdefault("id", _generate_record_id(prefix, payload.get("subject", ""), current_time))
    record_payload.setdefault("status", "active" if prefix == "g_" else "open")
    record_payload.setdefault("confidence", "medium")
    record_payload.setdefault("tags", [])
    record_payload.setdefault("source_refs", [])
    record_payload.setdefault("scope_reason", "")
    record_payload.setdefault("created_at", timestamp)
    record_payload["updated_at"] = timestamp
    if supersedes:
        record_payload["supersedes"] = list(supersedes)
    return MemoryRecord.from_dict(record_payload)


def _patch_record(record: MemoryRecord, patch: dict[str, Any], current_time: datetime) -> MemoryRecord:
    data = record.to_dict()
    for key, value in patch.items():
        data[key] = value
    data["updated_at"] = isoformat(current_time)
    return MemoryRecord.from_dict(data)


def _require_record(document: MemoryDocument, record_id: str) -> tuple[str, MemoryRecord]:
    match = get_record(document, record_id)
    if match is None:
        raise PatchApplyError(f"record not found: {record_id}")
    return match


def _require_record_in_documents(
    documents: dict[Path, MemoryDocument], record_id: str
) -> tuple[Path, MemoryRecord]:
    for path, document in documents.items():
        match = get_record(document, record_id)
        if match is not None:
            return path, match[1]
    raise PatchApplyError(f"record not found: {record_id}")


def _recent_documents_revision(documents: dict[Path, MemoryDocument]) -> int:
    return sum(document.revision for document in documents.values())


def _generate_record_id(prefix: str, subject: str, current_time: datetime) -> str:
    digest = sha256_text(f"{prefix}|{subject}|{current_time.timestamp()}")[:12]
    return f"{prefix}{digest}"
