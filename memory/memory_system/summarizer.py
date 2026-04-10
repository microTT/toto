from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from .config import MemoryConfig
from .constants import DEFAULT_PROMPT_VERSION, GLOBAL_SCOPE, LOCAL_RECENT_SCOPE
from .env_config import config_value, first_non_empty, load_dotenv_file, resolve_env_file
from .errors import PatchApplyError, SummarizerExecutionError
from .markdown_store import load_document
from .state_db import SummaryJob
from .validation import validate_patch_plan
from .workspace_store import iter_peer_memory_configs, iter_scoped_recent_documents

DEFAULT_SUMMARIZER_MODEL = "qwen3-max"
DEFAULT_SUMMARIZER_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_SUMMARIZER_ENDPOINT_MODE = "openai"
DEFAULT_SUMMARIZER_TEMPERATURE = 0.0
DEFAULT_SUMMARIZER_TIMEOUT_SECONDS = 120
DEFAULT_SUMMARIZER_MAX_OUTPUT_TOKENS = 4096
QWEN_BACKENDS = {"qwen"}
LEGACY_BACKEND_ALIASES = {"codex": "qwen"}
CREATE_RECORD_FIELDS = {
    "id",
    "type",
    "status",
    "confidence",
    "subject",
    "summary",
    "rationale",
    "next_use",
    "tags",
    "source_refs",
    "scope_reason",
    "pin_until",
    "supersedes",
    "superseded_by",
    "created_at",
    "updated_at",
}
STATUS_ALIASES = {
    "resolved": "closed",
    "complete": "closed",
    "completed": "closed",
    "done": "closed",
    "todo": "open",
    "pending": "open",
}
SUMMARIZER_SYSTEM_PROMPT = (
    "你是本地记忆系统的总结器。"
    "你的任务是根据当前对话增量和已有记忆，输出一个 JSON patch plan。"
    "不要输出 Markdown，不要输出解释，只能输出一个 JSON 对象。"
)
EXPLICIT_MEMORY_TERMS = ("记住", "remember")
REPO_SPECIFIC_TERMS = (
    "这个仓库",
    "当前仓库",
    "这个项目",
    "当前项目",
    "this repo",
    "this repository",
    "this project",
    "current repo",
    "current repository",
    "current project",
)
LONG_TERM_MARKERS = (
    "以后",
    "今后",
    "未来",
    "长期",
    "一直",
    "后续对话",
    "from now on",
    "going forward",
    "in future",
    "future conversations",
    "always",
)
PREFERENCE_CUES = (
    "偏好",
    "prefer",
    "默认",
    "default",
    "总是",
    "always",
    "不要",
    "不接受",
    "avoid",
    "don't",
    "do not",
    "只用",
    "only use",
)
IDENTITY_CUES = ("叫我", "call me", "我叫", "my name is", "以后叫", "call the assistant")
LANGUAGE_PREFERENCES = {
    "中文": "Chinese",
    "chinese": "Chinese",
    "英文": "English",
    "english": "English",
}
PACKAGE_MANAGERS = ("pnpm", "npm", "yarn", "bun")
MAX_CROSS_WORKSPACE_EXAMPLES = 3


@dataclass(slots=True)
class ExplicitGlobalCandidate:
    subject: str
    summary: str
    rationale: str
    next_use: str | None
    tags: list[str]
    type: str = "preference"


@dataclass(slots=True)
class SummarizerSettings:
    provider: str = "auto"
    model_name: str = DEFAULT_SUMMARIZER_MODEL
    base_url: str | None = DEFAULT_SUMMARIZER_BASE_URL
    api_key: str | None = None
    endpoint_mode: str = DEFAULT_SUMMARIZER_ENDPOINT_MODE
    timeout_seconds: int = DEFAULT_SUMMARIZER_TIMEOUT_SECONDS
    temperature: float = DEFAULT_SUMMARIZER_TEMPERATURE
    max_output_tokens: int = DEFAULT_SUMMARIZER_MAX_OUTPUT_TOKENS


def load_summarizer_settings(*, env_file: str | os.PathLike[str] | None = None) -> SummarizerSettings:
    dotenv = load_dotenv_file(resolve_env_file(env_file))
    provider = config_value("CODEX_MEMORY_SUMMARIZER_PROVIDER", dotenv, "auto").strip().lower() or "auto"
    endpoint_mode = (
        config_value(
            "CODEX_MEMORY_SUMMARIZER_ENDPOINT_MODE",
            dotenv,
            DEFAULT_SUMMARIZER_ENDPOINT_MODE,
        )
        .strip()
        .lower()
        or DEFAULT_SUMMARIZER_ENDPOINT_MODE
    )
    return SummarizerSettings(
        provider=provider,
        model_name=(
            config_value("CODEX_MEMORY_SUMMARIZER_MODEL", dotenv, DEFAULT_SUMMARIZER_MODEL).strip()
            or DEFAULT_SUMMARIZER_MODEL
        ),
        base_url=first_non_empty(
            config_value("CODEX_MEMORY_SUMMARIZER_BASE_URL", dotenv, None),
            config_value("CODEX_MEMORY_EMBEDDING_BASE_URL", dotenv, None),
            config_value("CODEX_MEMORY_SUMMARIZER_ENDPOINT", dotenv, None),
            DEFAULT_SUMMARIZER_BASE_URL,
        ),
        api_key=first_non_empty(
            config_value("CODEX_MEMORY_SUMMARIZER_API_KEY", dotenv, None),
            config_value("CODEX_MEMORY_EMBEDDING_API_KEY", dotenv, None),
        ),
        endpoint_mode=endpoint_mode,
        timeout_seconds=_parse_int_config(
            config_value(
                "CODEX_MEMORY_SUMMARIZER_TIMEOUT_SECONDS",
                dotenv,
                str(DEFAULT_SUMMARIZER_TIMEOUT_SECONDS),
            ),
            minimum=10,
            default=DEFAULT_SUMMARIZER_TIMEOUT_SECONDS,
        ),
        temperature=_parse_float_config(
            config_value(
                "CODEX_MEMORY_SUMMARIZER_TEMPERATURE",
                dotenv,
                str(DEFAULT_SUMMARIZER_TEMPERATURE),
            ),
            minimum=0.0,
            maximum=2.0,
            default=DEFAULT_SUMMARIZER_TEMPERATURE,
        ),
        max_output_tokens=_parse_int_config(
            config_value(
                "CODEX_MEMORY_SUMMARIZER_MAX_OUTPUT_TOKENS",
                dotenv,
                str(DEFAULT_SUMMARIZER_MAX_OUTPUT_TOKENS),
            ),
            minimum=256,
            default=DEFAULT_SUMMARIZER_MAX_OUTPUT_TOKENS,
        ),
    )


def build_deterministic_patch_plan(
    *,
    config: MemoryConfig,
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    user_messages = [
        str(event.get("user_message_delta", "")).strip()
        for event in events
        if str(event.get("user_message_delta", "")).strip()
    ]
    candidate = _extract_explicit_global_candidate(user_messages)
    if candidate is None:
        return None
    global_document = load_document(config.global_memory_path, GLOBAL_SCOPE)
    recent_documents = [
        load_document(path, LOCAL_RECENT_SCOPE) for path in sorted(config.recent_dir.glob("*.md"))
    ]
    global_ops: list[dict[str, Any]] = []
    local_ops: list[dict[str, Any]] = []
    global_op = _build_global_upsert_op(global_document, candidate)
    if global_op is not None:
        global_ops.append(global_op)
    next_step = _extract_next_step("\n".join(user_messages))
    if next_step:
        local_ops.append(_build_next_step_local_op(next_step))
    if not global_ops and not local_ops:
        decision = "noop"
        reason = "显式长期记忆请求对应的全局记录已是最新状态"
    else:
        decision = "write"
        reason = "检测到用户显式要求长期记住的偏好或身份事实"
    return {
        "decision": decision,
        "reason": reason,
        "base_revisions": {
            "global_revision": global_document.revision,
            "local_recent_revision": sum(document.revision for document in recent_documents),
        },
        "global_ops": global_ops,
        "local_ops": local_ops,
        "needs_manual_review": False,
    }


def build_cross_workspace_evidence(
    *,
    config: MemoryConfig,
    user_messages: list[str],
) -> dict[str, Any]:
    candidate = _extract_explicit_global_candidate(user_messages)
    peer_workspace_ids: set[str] = set()
    if candidate is None:
        for peer_config in iter_peer_memory_configs(config):
            if peer_config.workspace_instance_id != config.workspace_instance_id:
                peer_workspace_ids.add(peer_config.workspace_instance_id)
        return {"peer_workspace_count": len(peer_workspace_ids), "candidate_matches": []}

    global_hits = 0
    local_hits = 0
    examples: list[dict[str, str]] = []
    for peer_config in iter_peer_memory_configs(config):
        if peer_config.workspace_instance_id == config.workspace_instance_id:
            continue
        peer_workspace_ids.add(peer_config.workspace_instance_id)
        peer_global = load_document(peer_config.global_memory_path, GLOBAL_SCOPE)
        for record in peer_global.sections.get("Active", []):
            if _record_matches_candidate(record, candidate):
                global_hits += 1
                if len(examples) < MAX_CROSS_WORKSPACE_EXAMPLES:
                    examples.append(
                        {
                            "workspace_instance_id": peer_config.workspace_instance_id,
                            "scope": "global",
                            "subject": record.subject,
                            "summary": record.summary,
                        }
                    )
        for _, document in iter_scoped_recent_documents(peer_config):
            for section in ("Open", "Active"):
                for record in document.sections.get(section, []):
                    if _record_matches_candidate(record, candidate):
                        local_hits += 1
                        if len(examples) < MAX_CROSS_WORKSPACE_EXAMPLES:
                            examples.append(
                                {
                                    "workspace_instance_id": peer_config.workspace_instance_id,
                                    "scope": "local",
                                    "subject": record.subject,
                                    "summary": record.summary,
                                }
                            )
    return {
        "peer_workspace_count": len(peer_workspace_ids),
        "candidate_matches": [
            {
                "subject": candidate.subject,
                "summary": candidate.summary,
                "global_hit_count": global_hits,
                "local_hit_count": local_hits,
                "examples": examples,
            }
        ],
    }


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
    user_messages = [
        str(event.get("user_message_delta", "")).strip()
        for event in events
        if str(event.get("user_message_delta", "")).strip()
    ]
    cross_workspace_evidence = build_cross_workspace_evidence(config=config, user_messages=user_messages)
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
        "跨 workspace 相关证据：\n"
        f"{json.dumps(cross_workspace_evidence, indent=2, ensure_ascii=True)}\n\n"
        "策略：\n"
        "- 全局记忆仅保留可跨仓库复用的长期偏好与稳定约束。\n"
        "- 本地近期记忆保留仓库相关的阻塞项、TODO、失败结论和近期事实。\n"
        "- 如果用户显式要求“记住下一步/下次继续/remember next step”，优先写入本地 open task_context。\n"
        "- local 记录应上升为全局时用 promote；全局记录实际是仓库私有时用 demote。\n"
        "- 不要写入密钥、原始工具日志或无依据推测。\n"
        "- 必须严格遵守 base_revisions。\n\n"
        "基础版本：\n"
        f"{json.dumps(base_revisions, indent=2, ensure_ascii=True)}\n\n"
        "任务元数据：\n"
        f"{json.dumps({'job_id': job.id, 'prompt_version': DEFAULT_PROMPT_VERSION}, indent=2, ensure_ascii=True)}\n\n"
        "输出要求：\n"
        "返回 patch plan，包含 decision、reason、base_revisions、global_ops、local_ops、needs_manual_review。\n"
        "允许动作：create、update、supersede、delete、pin、promote、demote。\n"
        "- create 动作必须包含 record 对象；不要只返回 content 或 summary 字符串。\n"
    )


def summarize_job(
    *,
    config: MemoryConfig,
    job: SummaryJob,
    events: list[dict[str, Any]],
    backend: str = "qwen",
) -> dict[str, Any]:
    deterministic = build_deterministic_patch_plan(config=config, events=events)
    if deterministic is not None:
        return deterministic
    resolved_backend = LEGACY_BACKEND_ALIASES.get(backend, backend)
    if resolved_backend == "heuristic":
        return heuristic_patch_plan(config=config, events=events)
    if resolved_backend not in QWEN_BACKENDS:
        raise PatchApplyError(f"unknown summarizer backend: {backend}")
    return _run_qwen_completion(config=config, job=job, events=events)


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
        local_ops.append(_build_next_step_local_op(next_step))
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


def _run_qwen_completion(*, config: MemoryConfig, job: SummaryJob, events: list[dict[str, Any]]) -> dict[str, Any]:
    import requests

    settings = load_summarizer_settings()
    if settings.provider not in {"auto", "qwen_openai"}:
        raise SummarizerExecutionError(f"unsupported summarizer provider: {settings.provider}")
    if not _remote_summarizer_enabled(settings):
        raise SummarizerExecutionError(
            "Qwen summarizer is not configured. Set CODEX_MEMORY_SUMMARIZER_API_KEY "
            "or switch the worker backend to heuristic."
        )
    prompt = build_patch_prompt(config=config, job=job, events=events)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
    }
    payload: dict[str, Any] = {
        "model": settings.model_name,
        "messages": [
            {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": settings.temperature,
        "max_tokens": settings.max_output_tokens,
    }
    try:
        response = requests.post(
            _summarizer_request_url(settings),
            json=payload,
            headers=headers,
            timeout=settings.timeout_seconds,
        )
        response.raise_for_status()
    except Exception as exc:
        raise SummarizerExecutionError(f"qwen summarizer request failed: {exc}") from exc
    try:
        content = _extract_completion_content(response.json())
        patch_plan = _parse_patch_plan_payload(content)
    except SummarizerExecutionError:
        raise
    except Exception as exc:
        raise SummarizerExecutionError(f"qwen summarizer response parse failed: {exc}") from exc
    normalized = _normalize_model_patch_plan(patch_plan)
    try:
        validate_patch_plan(normalized)
    except PatchApplyError as exc:
        raise SummarizerExecutionError(f"qwen summarizer returned invalid patch plan: {exc}") from exc
    return normalized


def _normalize_model_patch_plan(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["global_ops"] = [_normalize_model_op(op, scope="global") for op in payload.get("global_ops", [])]
    normalized["local_ops"] = [_normalize_model_op(op, scope="local") for op in payload.get("local_ops", [])]
    normalized["decision"] = _normalize_decision(normalized.get("decision"), payload=normalized)
    return normalized


def _normalize_model_op(op: dict[str, Any], *, scope: str) -> dict[str, Any]:
    cleaned = {key: value for key, value in op.items() if value is not None}
    action = cleaned.get("action")
    record_scope = scope
    replacement_scope = scope
    if action == "promote":
        replacement_scope = "global"
    elif action == "demote":
        replacement_scope = "local"
    if isinstance(cleaned.get("id"), str) and "target_id" not in cleaned:
        cleaned["target_id"] = cleaned.pop("id")
    if isinstance(cleaned.get("fields"), dict):
        if action == "create" and "record" not in cleaned:
            cleaned["record"] = cleaned.pop("fields")
        elif action == "update" and "record_patch" not in cleaned:
            cleaned["record_patch"] = cleaned.pop("fields")
    if action == "update" and "record_patch" not in cleaned and isinstance(cleaned.get("record"), dict):
        cleaned["record_patch"] = cleaned.pop("record")
    if isinstance(cleaned.get("replacement"), dict) and "replacement_record" not in cleaned:
        cleaned["replacement_record"] = cleaned.pop("replacement")
    if action in {"supersede", "promote", "demote"} and isinstance(cleaned.get("record"), dict):
        cleaned.setdefault("replacement_record", cleaned.pop("record"))
    if action == "delete" and "tombstone" not in cleaned and isinstance(cleaned.get("reason"), str):
        cleaned["tombstone"] = {"reason": cleaned.pop("reason"), "source_refs": []}
    if action == "pin" and "pin" not in cleaned and isinstance(cleaned.get("pin_until"), str):
        cleaned["pin"] = {"pin_until": cleaned.pop("pin_until")}
    if action == "create" and "record" not in cleaned:
        record = _normalize_create_record(cleaned, scope=record_scope)
        if record is not None:
            cleaned["record"] = record
    if isinstance(cleaned.get("record"), dict):
        cleaned["record"] = _normalize_record_payload(
            cleaned["record"],
            scope=record_scope,
            require_status=action == "create",
        )
    if isinstance(cleaned.get("record_patch"), dict):
        cleaned["record_patch"] = _normalize_record_patch_payload(cleaned["record_patch"])
    if isinstance(cleaned.get("replacement_record"), dict):
        cleaned["replacement_record"] = _normalize_record_payload(
            cleaned["replacement_record"],
            scope=replacement_scope,
            require_status=False,
        )
    for field in ("record", "record_patch", "replacement_record", "tombstone", "pin"):
        value = cleaned.get(field)
        if isinstance(value, dict):
            cleaned[field] = {
                nested_key: nested_value
                for nested_key, nested_value in value.items()
                if nested_value is not None
            }
    return cleaned


def _normalize_create_record(op: dict[str, Any], *, scope: str) -> dict[str, Any] | None:
    record = {field: op[field] for field in CREATE_RECORD_FIELDS if field in op}
    content = _extract_create_summary(op)
    if content is not None and "summary" not in record:
        record["summary"] = content
    if not record:
        return None
    return _normalize_record_payload(record, scope=scope, require_status=True)


def _normalize_record_payload(
    record: dict[str, Any],
    *,
    scope: str,
    require_status: bool,
) -> dict[str, Any]:
    normalized = dict(record)
    content = _extract_create_summary(normalized)
    if content is not None and "summary" not in normalized:
        normalized["summary"] = content
    if "type" not in normalized:
        normalized["type"] = "task_context" if scope == "local" else "fact"
    if isinstance(normalized.get("status"), str):
        normalized["status"] = _normalize_status(normalized["status"])
    if require_status and "status" not in normalized:
        normalized["status"] = "open" if scope == "local" else "active"
    if "confidence" not in normalized:
        normalized["confidence"] = "medium"
    if "subject" not in normalized:
        normalized["subject"] = _summarize_subject(normalized.get("summary"), scope=scope)
    if "tags" not in normalized or not isinstance(normalized["tags"], list):
        normalized["tags"] = []
    if "source_refs" not in normalized or not isinstance(normalized["source_refs"], list):
        normalized["source_refs"] = []
    if "scope_reason" not in normalized:
        normalized["scope_reason"] = (
            "repo-specific and near-term" if scope == "local" else "cross-workspace and durable"
        )
    return normalized


def _normalize_record_patch_payload(record_patch: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record_patch)
    content = _extract_create_summary(normalized)
    if content is not None and "summary" not in normalized:
        normalized["summary"] = content
    if isinstance(normalized.get("status"), str):
        normalized["status"] = _normalize_status(normalized["status"])
    return normalized


def _extract_create_summary(op: dict[str, Any]) -> str | None:
    for key in ("summary", "content", "text", "message", "note"):
        value = op.get(key)
        if isinstance(value, str):
            compact = " ".join(value.split())
            if compact:
                return compact
    return None


def _summarize_subject(summary: Any, *, scope: str) -> str:
    if isinstance(summary, str):
        compact = " ".join(summary.split())
        if compact:
            return compact if len(compact) <= 60 else f"{compact[:57]}..."
    return "本地记忆" if scope == "local" else "全局记忆"


def _normalize_status(status: str) -> str:
    compact = status.strip().lower()
    return STATUS_ALIASES.get(compact, compact)


def _normalize_decision(decision: Any, *, payload: dict[str, Any]) -> str:
    if decision == "noop":
        return "noop"
    if decision == "write":
        return "write"
    if isinstance(decision, str) and decision in {
        "create",
        "update",
        "supersede",
        "delete",
        "pin",
        "promote",
        "demote",
    }:
        return "write"
    if payload.get("global_ops") or payload.get("local_ops"):
        return "write"
    return "noop"


def _build_next_step_local_op(next_step: str) -> dict[str, Any]:
    return {
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


def _build_global_upsert_op(
    document,
    candidate: ExplicitGlobalCandidate,
) -> dict[str, Any] | None:
    record_payload = {
        "type": candidate.type,
        "status": "active",
        "confidence": "high",
        "subject": candidate.subject,
        "summary": candidate.summary,
        "tags": list(candidate.tags),
        "source_refs": [],
        "scope_reason": "cross-workspace and durable",
        "rationale": candidate.rationale,
        "next_use": candidate.next_use,
    }
    existing = _find_active_global_record(document, candidate.subject)
    if existing is None:
        return {"action": "create", "record": record_payload}
    if _record_matches_payload(existing, record_payload):
        return None
    return {"action": "update", "target_id": existing.id, "record_patch": record_payload}


def _find_active_global_record(document, subject: str):
    normalized_subject = _normalize_text(subject)
    for record in document.sections.get("Active", []):
        if _normalize_text(record.subject) == normalized_subject:
            return record
    return None


def _record_matches_payload(record, payload: dict[str, Any]) -> bool:
    current = record.to_dict()
    for key, value in payload.items():
        if current.get(key) != value:
            return False
    return True


def _record_matches_candidate(record, candidate: ExplicitGlobalCandidate) -> bool:
    if _normalize_text(record.subject) == _normalize_text(candidate.subject):
        return True
    haystack = _normalize_text(f"{record.subject} {record.summary}")
    for token in _match_tokens(candidate.summary):
        if token in haystack:
            return True
    return False


def _extract_explicit_global_candidate(user_messages: list[str]) -> ExplicitGlobalCandidate | None:
    for message in reversed(user_messages):
        candidate = _extract_explicit_global_candidate_from_message(message)
        if candidate is not None:
            return candidate
    return None


def _extract_explicit_global_candidate_from_message(message: str) -> ExplicitGlobalCandidate | None:
    compact = " ".join(message.split())
    if not compact:
        return None
    lowered = compact.lower()
    if not any(term in lowered for term in EXPLICIT_MEMORY_TERMS):
        return None
    if _extract_next_step(compact, lowered=lowered):
        return None
    if any(term in lowered for term in REPO_SPECIFIC_TERMS):
        return None
    cleaned = _strip_memory_prefix(compact)
    if not cleaned:
        return None
    candidate = _extract_assistant_name_candidate(cleaned)
    if candidate is not None:
        return candidate
    candidate = _extract_user_name_candidate(cleaned)
    if candidate is not None:
        return candidate
    candidate = _extract_package_manager_candidate(cleaned)
    if candidate is not None:
        return candidate
    candidate = _extract_response_language_candidate(cleaned)
    if candidate is not None:
        return candidate
    if _looks_like_durable_preference_or_identity(cleaned):
        return ExplicitGlobalCandidate(
            subject="Long-term user preference",
            summary=cleaned,
            rationale=f"Explicit user instruction: {compact}",
            next_use="Apply this preference in future conversations unless the repository explicitly requires otherwise.",
            tags=["explicit-memory", "preference"],
        )
    return None


def _strip_memory_prefix(text: str) -> str:
    patterns = (
        r"^\s*(?:请|麻烦|帮我)?\s*(?:长期)?记住(?:一下)?[，,:：\s]*",
        r"^\s*remember(?:\s+that)?[，,:：\s]*",
    )
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, count=1, flags=re.IGNORECASE)
    return cleaned.strip(" ，。,.!！?？:：;；")


def _extract_assistant_name_candidate(text: str) -> ExplicitGlobalCandidate | None:
    patterns = (
        re.compile(r"(?:你|助手)(?:以后|今后|之后|未来|从现在起)?(?:都)?叫\s*([^\s，。,.!?！？；;]+)"),
        re.compile(r"(?:call(?:\s+the)?\s+assistant|call\s+you)\s+([A-Za-z][A-Za-z0-9_-]{0,31})", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        name = match.group(1).strip("“”\"'")
        if not name:
            continue
        if _contains_cjk(text):
            summary = f"用户希望在后续对话中把助手称为 {name}。"
            next_use = f"后续对话中将助手称为 {name}。"
        else:
            summary = f"User wants the assistant to be called {name} in future conversations."
            next_use = f"Refer to the assistant as {name} in future conversations."
        return ExplicitGlobalCandidate(
            subject="Assistant name preference",
            summary=summary,
            rationale=f"Explicit user instruction: {text}",
            next_use=next_use,
            tags=["assistant", "naming", "identity"],
        )
    return None


def _extract_user_name_candidate(text: str) -> ExplicitGlobalCandidate | None:
    patterns = (
        re.compile(r"(?:叫我|请叫我)\s*([^\s，。,.!?！？；;]+)"),
        re.compile(r"(?:call me|my name is)\s+([A-Za-z][A-Za-z0-9_-]{0,31})", re.IGNORECASE),
        re.compile(r"我叫\s*([^\s，。,.!?！？；;]+)"),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        name = match.group(1).strip("“”\"'")
        if not name:
            continue
        if _contains_cjk(text):
            summary = f"用户希望在后续对话中被称为 {name}。"
            next_use = f"后续对话中使用 {name} 称呼用户。"
        else:
            summary = f"User prefers to be addressed as {name} in future conversations."
            next_use = f"Address the user as {name} in future conversations."
        return ExplicitGlobalCandidate(
            subject="User name preference",
            summary=summary,
            rationale=f"Explicit user instruction: {text}",
            next_use=next_use,
            tags=["user", "naming", "identity"],
        )
    return None


def _extract_package_manager_candidate(text: str) -> ExplicitGlobalCandidate | None:
    lowered = text.lower()
    if not any(manager in lowered for manager in PACKAGE_MANAGERS):
        return None
    if not any(marker in lowered for marker in LONG_TERM_MARKERS + PREFERENCE_CUES):
        return None
    preferred = next(manager for manager in PACKAGE_MANAGERS if manager in lowered)
    summary = (
        f"Future JavaScript/TypeScript repositories should default to {preferred} unless the repository explicitly requires another package manager."
    )
    return ExplicitGlobalCandidate(
        subject="package manager preference",
        summary=summary,
        rationale=f"Explicit user instruction: {text}",
        next_use=f"Default to {preferred} for future JavaScript/TypeScript work unless the repository explicitly requires another package manager.",
        tags=["tooling", "package-manager", preferred],
    )


def _extract_response_language_candidate(text: str) -> ExplicitGlobalCandidate | None:
    lowered = text.lower()
    if not any(term in lowered for term in ("回复", "回答", "respond", "reply")):
        return None
    preferred_language = None
    for term, label in LANGUAGE_PREFERENCES.items():
        if term in lowered:
            preferred_language = label
            break
    if preferred_language is None:
        return None
    summary = f"Prefer responses in {preferred_language}."
    if preferred_language == "Chinese":
        summary = "用户偏好使用中文回复。"
    elif preferred_language == "English":
        summary = "用户偏好使用英文回复。"
    return ExplicitGlobalCandidate(
        subject="Response language preference",
        summary=summary,
        rationale=f"Explicit user instruction: {text}",
        next_use=f"Respond in {preferred_language} unless the user overrides it in the current turn.",
        tags=["language", "response-style", preferred_language.lower()],
    )


def _looks_like_durable_preference_or_identity(text: str) -> bool:
    lowered = text.lower()
    has_durable_marker = any(marker in lowered for marker in LONG_TERM_MARKERS)
    has_preference_cue = any(marker in lowered for marker in PREFERENCE_CUES + IDENTITY_CUES)
    return has_durable_marker and has_preference_cue


def _contains_cjk(text: str) -> bool:
    return re.search(r"[\u4e00-\u9fff]", text) is not None


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _match_tokens(text: str) -> list[str]:
    lowered = _normalize_text(text)
    tokens = set(re.findall(r"[a-z0-9_+-]{3,}|[\u4e00-\u9fff]{2,}", lowered))
    return sorted(tokens)


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


def _remote_summarizer_enabled(settings: SummarizerSettings) -> bool:
    if not settings.base_url:
        return False
    if settings.endpoint_mode == "openai" and not settings.api_key:
        return False
    return True


def _summarizer_request_url(settings: SummarizerSettings) -> str:
    if not settings.base_url:
        raise SummarizerExecutionError("Qwen summarizer base URL is not configured")
    url = settings.base_url.rstrip("/")
    if settings.endpoint_mode == "openai":
        return url if url.endswith("/chat/completions") else f"{url}/chat/completions"
    return url


def _extract_completion_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SummarizerExecutionError(f"unsupported completion response shape: {_truncate_json(payload)}")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise SummarizerExecutionError(f"completion response is missing message: {_truncate_json(payload)}")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "".join(parts)
    raise SummarizerExecutionError(f"completion response is missing text content: {_truncate_json(payload)}")


def _parse_patch_plan_payload(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = _strip_code_fences(cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise SummarizerExecutionError(f"summarizer did not return JSON: {cleaned[:400]}")
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise SummarizerExecutionError(f"summarizer returned invalid JSON: {cleaned[:400]}") from exc
    if not isinstance(payload, dict):
        raise SummarizerExecutionError("summarizer payload must be a JSON object")
    return payload


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```json"):
        stripped = stripped[len("```json") :]
    elif stripped.startswith("```"):
        stripped = stripped[len("```") :]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def _truncate_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)[:300]


def _parse_int_config(raw: str, *, minimum: int, default: int) -> int:
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _parse_float_config(raw: str, *, minimum: float, maximum: float, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))
