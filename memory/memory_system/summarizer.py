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
    transcript_delta = "\n".join(delta_lines) or "（无可用的对话增量）"
    base_revisions = {
        "global_revision": global_document.revision,
        "local_recent_revision": sum(document.revision for document in recent_documents),
    }
    return (
        "任务说明：\n"
        "你是记忆总结器。仅输出 JSON，不要直接修改文件。\n\n"
        "对话增量：\n"
        f"{transcript_delta}\n\n"
        "当前激活的全局记忆：\n"
        f"{json.dumps(active_global, indent=2, ensure_ascii=True)}\n\n"
        "当前激活的本地近期记忆：\n"
        f"{json.dumps(active_local, indent=2, ensure_ascii=True)}\n\n"
        "策略：\n"
        "- 全局记忆仅保留可跨仓库复用的长期偏好与稳定约束。\n"
        "- 本地近期记忆保留仓库相关的阻塞项、TODO、失败结论和近期事实。\n"
        "- local 记录应上升为全局时用 promote；全局记录实际是仓库私有时用 demote。\n"
        "- 不要写入密钥、原始工具日志或无依据推测。\n"
        "- 必须严格遵守 base_revisions。\n\n"
        "基础版本：\n"
        f"{json.dumps(base_revisions, indent=2, ensure_ascii=True)}\n\n"
        "输出要求：\n"
        "返回 patch plan，包含 decision、reason、base_revisions、global_ops、local_ops、needs_manual_review。\n"
        "允许动作：create、update、supersede、delete、pin、promote、demote。\n"
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
    reason = "没有可写入的记忆候选"
    lowered = combined.lower()
    next_step = _extract_next_step(combined, lowered=lowered)
    if next_step:
        decision = "write"
        reason = "检测到显式的下一步记忆请求"
        local_ops.append(
            {
                "action": "create",
                "record": {
                    "type": "task_context",
                    "status": "open",
                    "subject": "下一步",
                    "summary": next_step,
                    "confidence": "high",
                    "tags": ["todo", "next-step"],
                    "source_refs": [],
                    "scope_reason": "仓库内近期待办",
                    "next_use": f"恢复当前仓库工作时优先执行：{next_step}",
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


def _extract_next_step(combined: str, *, lowered: str | None = None) -> str | None:
    lowered_text = lowered or combined.lower()
    english_markers = ("remember next step:", "remember next step：")
    chinese_markers = (
        "记住下一步:",
        "记住下一步：",
        "下一步:",
        "下一步：",
        "下次继续:",
        "下次继续：",
    )
    for marker in english_markers:
        start = lowered_text.find(marker)
        if start >= 0:
            return combined[start + len(marker) :].strip() or None
    for marker in chinese_markers:
        start = combined.find(marker)
        if start >= 0:
            return combined[start + len(marker) :].strip() or None
    return None
