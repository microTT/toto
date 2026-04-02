from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .config import MemoryConfig
from .constants import DEFAULT_PROMPT_VERSION, GLOBAL_SCOPE, LOCAL_RECENT_SCOPE, SCHEMA_DIR
from .markdown_store import load_document
from .patch_applier import PatchApplyError
from .state_db import SummaryJob
from .utils import isoformat


def build_patch_prompt(
    *,
    config: MemoryConfig,
    job: SummaryJob,
    events: list[dict[str, Any]],
) -> str:
    global_document = load_document(config.global_memory_path, GLOBAL_SCOPE)
    recent_documents = [
        load_document(path, LOCAL_RECENT_SCOPE) for path in sorted(config.recent_dir.glob("*.md"))
    ]
    active_global = [
        record.to_dict() for record in global_document.sections.get("Active", [])
    ]
    active_local: list[dict[str, Any]] = []
    for document in recent_documents:
        for section in ("Open", "Active"):
            active_local.extend(record.to_dict() for record in document.sections.get(section, []))
    delta_lines: list[str] = []
    for event in events:
        user_message = event.get("user_message_delta") or ""
        assistant_message = event.get("assistant_message_delta") or ""
        if user_message:
            delta_lines.append(f"USER: {user_message}")
        if assistant_message:
            delta_lines.append(f"ASSISTANT: {assistant_message}")
    transcript_delta = "\n".join(delta_lines) or "(no transcript delta available)"
    base_revisions = {
        "global_revision": global_document.revision,
        "local_recent_revision": sum(document.revision for document in recent_documents),
    }
    return (
        "Task brief:\n"
        "You are a memory summarizer. Output JSON only. Do not modify files directly.\n\n"
        "Transcript delta:\n"
        f"{transcript_delta}\n\n"
        "Current active global memory:\n"
        f"{json.dumps(active_global, indent=2, ensure_ascii=True)}\n\n"
        "Current active local recent memory:\n"
        f"{json.dumps(active_local, indent=2, ensure_ascii=True)}\n\n"
        "Policy:\n"
        "- Keep only durable preferences/constraints in global memory.\n"
        "- Keep repo-specific blockers, TODOs, failed attempts, and near-term facts in local recent memory.\n"
        "- Use promote when a local record should become global; use demote when a global record is actually repo-specific.\n"
        "- Never store secrets, raw tool logs, or unsupported speculation.\n"
        "- Respect the base_revisions exactly.\n\n"
        "Base revisions:\n"
        f"{json.dumps(base_revisions, indent=2, ensure_ascii=True)}\n\n"
        "Output schema:\n"
        "Return a patch plan with decision, reason, base_revisions, global_ops, local_ops, and needs_manual_review.\n"
        "Allowed actions: create, update, supersede, delete, pin, promote, demote.\n"
    )


def summarize_job(
    *,
    config: MemoryConfig,
    job: SummaryJob,
    events: list[dict[str, Any]],
    backend: str = "codex",
) -> dict[str, Any]:
    if backend == "heuristic":
        return heuristic_patch_plan(config=config, events=events)
    if backend != "codex":
        raise PatchApplyError(f"unknown summarizer backend: {backend}")
    return _run_codex_exec(config=config, job=job, events=events)


def heuristic_patch_plan(*, config: MemoryConfig, events: list[dict[str, Any]]) -> dict[str, Any]:
    global_document = load_document(config.global_memory_path, GLOBAL_SCOPE)
    recent_documents = [
        load_document(path, LOCAL_RECENT_SCOPE) for path in sorted(config.recent_dir.glob("*.md"))
    ]
    local_revision = sum(document.revision for document in recent_documents)
    combined = "\n".join(
        filter(
            None,
            [event.get("user_message_delta", "") for event in events]
            + [event.get("assistant_message_delta", "") for event in events],
        )
    )
    decision = "noop"
    local_ops: list[dict[str, Any]] = []
    reason = "no durable memory candidate"
    lowered = combined.lower()
    if "remember next step:" in lowered:
        decision = "write"
        reason = "explicit remember next step"
        next_step = combined.split(":", 1)[1].strip()
        local_ops.append(
            {
                "action": "create",
                "record": {
                    "type": "task_context",
                    "status": "open",
                    "subject": "next step",
                    "summary": next_step,
                    "confidence": "high",
                    "tags": ["todo"],
                    "source_refs": [],
                    "scope_reason": "repo-specific and near-term",
                    "next_use": next_step,
                },
            }
        )
    return {
        "decision": decision,
        "reason": reason,
        "base_revisions": {
            "global_revision": global_document.revision,
            "local_recent_revision": local_revision,
        },
        "global_ops": [],
        "local_ops": local_ops,
        "needs_manual_review": False,
    }


def _run_codex_exec(*, config: MemoryConfig, job: SummaryJob, events: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = build_patch_prompt(config=config, job=job, events=events)
    schema_path = SCHEMA_DIR / "memory_patch.schema.json"
    with TemporaryDirectory(prefix="memoryd-") as temp_dir:
        temp_root = Path(temp_dir)
        prompt_path = temp_root / "prompt.txt"
        result_path = temp_root / "result.json"
        run_log_path = temp_root / "run.jsonl"
        prompt_path.write_text(prompt, encoding="utf-8")
        env = os.environ.copy()
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEX_SESSION_ID", None)
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(result_path),
            "--output-schema",
            str(schema_path),
            "-c",
            "features.codex_hooks=false",
            "-C",
            str(config.workspace_root),
            "-",
        ]
        with prompt_path.open("r", encoding="utf-8") as handle, run_log_path.open(
            "w", encoding="utf-8"
        ) as run_log:
            completed = subprocess.run(
                command,
                stdin=handle,
                stdout=run_log,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                check=False,
            )
        if completed.returncode != 0:
            raise PatchApplyError(
                f"codex exec summarizer failed with exit code {completed.returncode}: {completed.stderr.strip()}"
            )
        return _normalize_model_patch_plan(json.loads(result_path.read_text(encoding="utf-8")))


def _normalize_model_patch_plan(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["global_ops"] = [_normalize_model_op(op) for op in payload.get("global_ops", [])]
    normalized["local_ops"] = [_normalize_model_op(op) for op in payload.get("local_ops", [])]
    return normalized


def _normalize_model_op(op: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in op.items() if value is not None}
    for field in ("record", "record_patch", "replacement_record", "tombstone", "pin"):
        value = cleaned.get(field)
        if isinstance(value, dict):
            cleaned[field] = {nested_key: nested_value for nested_key, nested_value in value.items() if nested_value is not None}
    return cleaned
