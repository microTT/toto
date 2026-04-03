from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .config import MemoryConfig
from .constants import DEFAULT_PROMPT_VERSION, GLOBAL_SCOPE, LOCAL_RECENT_SCOPE
from .env_config import config_value, first_non_empty, load_dotenv_file, resolve_env_file
from .errors import PatchApplyError, SummarizerExecutionError
from .markdown_store import load_document
from .state_db import SummaryJob
from .validation import validate_patch_plan

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
