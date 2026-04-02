from __future__ import annotations

import re
from typing import Any

from .constants import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_DELETED,
    STATUS_OPEN,
    STATUS_SUPERSEDED,
)
from .errors import PatchApplyError

ALLOWED_STATUSES = {
    STATUS_ACTIVE,
    STATUS_OPEN,
    STATUS_CLOSED,
    STATUS_SUPERSEDED,
    STATUS_DELETED,
}

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret)\b\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._-]{10,}\b", re.IGNORECASE),
]


def validate_patch_plan(patch_plan: dict[str, Any]) -> None:
    decision = patch_plan.get("decision")
    if decision not in {"noop", "write"}:
        raise PatchApplyError("patch plan decision must be noop or write")
    if not isinstance(patch_plan.get("reason"), str):
        raise PatchApplyError("patch plan reason must be a string")
    base_revisions = patch_plan.get("base_revisions")
    if not isinstance(base_revisions, dict):
        raise PatchApplyError("patch plan must include base_revisions")
    for key in ("global_revision", "local_recent_revision"):
        if not isinstance(base_revisions.get(key), int) or base_revisions[key] < 0:
            raise PatchApplyError(f"base_revisions.{key} must be a non-negative integer")
    global_ops = patch_plan.get("global_ops")
    local_ops = patch_plan.get("local_ops")
    if not isinstance(global_ops, list) or not isinstance(local_ops, list):
        raise PatchApplyError("global_ops and local_ops must be arrays")
    if not isinstance(patch_plan.get("needs_manual_review"), bool):
        raise PatchApplyError("needs_manual_review must be a boolean")
    for op in global_ops:
        _validate_op(op, scope="global")
    for op in local_ops:
        _validate_op(op, scope="local")
    if decision == "noop" and (global_ops or local_ops):
        raise PatchApplyError("noop patch plan must not contain write operations")


def reject_secrets_in_patch_plan(patch_plan: dict[str, Any]) -> None:
    for op_list_name in ("global_ops", "local_ops"):
        for op in patch_plan.get(op_list_name, []):
            for field_name in ("record", "record_patch", "replacement_record", "tombstone", "pin"):
                payload = op.get(field_name)
                if isinstance(payload, dict):
                    _reject_secret_payload(payload)


def _validate_op(op: Any, *, scope: str) -> None:
    if not isinstance(op, dict):
        raise PatchApplyError("operation must be an object")
    action = op.get("action")
    if action == "create":
        record = op.get("record")
        if not isinstance(record, dict):
            raise PatchApplyError("create op requires record")
        _validate_record(record, require_status=False, scope=scope)
        return
    if action == "update":
        if not isinstance(op.get("target_id"), str) or not op["target_id"]:
            raise PatchApplyError("update op requires target_id")
        record_patch = op.get("record_patch")
        if not isinstance(record_patch, dict) or not record_patch:
            raise PatchApplyError("update op requires non-empty record_patch")
        _validate_record_patch(record_patch)
        return
    if action == "supersede":
        if not isinstance(op.get("target_id"), str) or not op["target_id"]:
            raise PatchApplyError("supersede op requires target_id")
        replacement = op.get("replacement_record")
        if not isinstance(replacement, dict):
            raise PatchApplyError("supersede op requires replacement_record")
        _validate_record(replacement, require_status=False, scope=scope)
        return
    if action == "delete":
        if not isinstance(op.get("target_id"), str) or not op["target_id"]:
            raise PatchApplyError("delete op requires target_id")
        tombstone = op.get("tombstone")
        if not isinstance(tombstone, dict) or not isinstance(tombstone.get("reason"), str):
            raise PatchApplyError("delete op requires tombstone.reason")
        return
    if action == "pin":
        if scope != "local":
            raise PatchApplyError("pin op is only valid for local scope")
        if not isinstance(op.get("target_id"), str) or not op["target_id"]:
            raise PatchApplyError("pin op requires target_id")
        pin = op.get("pin")
        if not isinstance(pin, dict) or not isinstance(pin.get("pin_until"), str):
            raise PatchApplyError("pin op requires pin.pin_until")
        return
    if action == "promote":
        if scope != "local":
            raise PatchApplyError("promote op is only valid for local scope")
        if not isinstance(op.get("target_id"), str) or not op["target_id"]:
            raise PatchApplyError("promote op requires target_id")
        replacement = op.get("replacement_record")
        if not isinstance(replacement, dict):
            raise PatchApplyError("promote op requires replacement_record")
        _validate_record(replacement, require_status=False, scope="global")
        return
    if action == "demote":
        if scope != "global":
            raise PatchApplyError("demote op is only valid for global scope")
        if not isinstance(op.get("target_id"), str) or not op["target_id"]:
            raise PatchApplyError("demote op requires target_id")
        replacement = op.get("replacement_record")
        if not isinstance(replacement, dict):
            raise PatchApplyError("demote op requires replacement_record")
        _validate_record(replacement, require_status=False, scope="local")
        return
    raise PatchApplyError(f"unsupported operation action: {action}")


def _validate_record(record: dict[str, Any], *, require_status: bool, scope: str) -> None:
    required = ["type", "subject", "summary", "confidence", "tags", "source_refs", "scope_reason"]
    for key in required:
        if key not in record:
            raise PatchApplyError(f"record missing required field: {key}")
    if require_status and "status" not in record:
        raise PatchApplyError("record missing required field: status")
    if "status" in record and record["status"] not in ALLOWED_STATUSES:
        raise PatchApplyError(f"record has invalid status: {record['status']}")
    if record["confidence"] not in {"high", "medium", "low"}:
        raise PatchApplyError(f"record has invalid confidence: {record['confidence']}")
    if not isinstance(record["tags"], list) or not isinstance(record["source_refs"], list):
        raise PatchApplyError("record tags and source_refs must be arrays")
    if scope == "global" and record.get("status") == STATUS_OPEN:
        raise PatchApplyError("global records cannot have open status")


def _validate_record_patch(record_patch: dict[str, Any]) -> None:
    if "status" in record_patch and record_patch["status"] not in ALLOWED_STATUSES:
        raise PatchApplyError(f"record patch has invalid status: {record_patch['status']}")
    if "confidence" in record_patch and record_patch["confidence"] not in {"high", "medium", "low"}:
        raise PatchApplyError(f"record patch has invalid confidence: {record_patch['confidence']}")
    if "tags" in record_patch and not isinstance(record_patch["tags"], list):
        raise PatchApplyError("record patch tags must be an array")
    if "source_refs" in record_patch and not isinstance(record_patch["source_refs"], list):
        raise PatchApplyError("record patch source_refs must be an array")


def _reject_secret_payload(payload: dict[str, Any]) -> None:
    for value in payload.values():
        if isinstance(value, dict):
            _reject_secret_payload(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    _reject_secret_text(item)
        elif isinstance(value, str):
            _reject_secret_text(value)


def _reject_secret_text(text: str) -> None:
    lowered = text.lower()
    if "-----begin" in lowered and "private key" in lowered:
        raise PatchApplyError("secret-like private key material detected in patch plan")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise PatchApplyError("secret-like token or credential detected in patch plan")
