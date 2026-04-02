from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from .config import MemoryConfig
from .constants import GLOBAL_SCOPE, LOCAL_ARCHIVE_SCOPE, LOCAL_RECENT_SCOPE
from .markdown_store import increment_revision, load_document, save_document
from .models import MemoryDocument, MemoryRecord


_ASSISTANT_NAME_SUMMARY_RE = re.compile(
    r"^User wants the assistant to be called (?P<name>.+?) in future conversations\.$"
)
_ASSISTANT_NAME_NEXT_USE_RE = re.compile(
    r"^Refer to the assistant as (?P<name>.+?) in future conversations\.$"
)
_NEAR_TERM_STEP_RE = re.compile(r"^Near-term repo-specific next step: (?P<step>.+?)\.?$")
_ZH_NEAR_TERM_STEP_RE = re.compile(r"^当前仓库近期下一步：(?P<step>.+?)。?$")


def _translate_action_phrase(value: str) -> str:
    translations = {
        "run a launchd daemon smoke test": "执行一次 Launchd 常驻进程冒烟测试",
        "revisit the failing auth snapshot": "重新检查失败的 auth 快照",
    }
    normalized = value.strip().rstrip(".")
    return translations.get(normalized, normalized)


def migrate_records_to_zh(config: MemoryConfig) -> dict[str, object]:
    changed_paths: list[str] = []
    changed_records = 0
    scanned_records = 0
    for path, scope in _iter_documents(config):
        document = load_document(path, scope)
        migrated_document, document_changed, document_record_changes, document_record_total = _migrate_document(
            document
        )
        scanned_records += document_record_total
        changed_records += document_record_changes
        if document_changed:
            save_document(path, increment_revision(migrated_document))
            changed_paths.append(str(path))
    return {
        "changed_paths": changed_paths,
        "changed_records": changed_records,
        "scanned_records": scanned_records,
    }


def _iter_documents(config: MemoryConfig) -> list[tuple[Path, str]]:
    documents: list[tuple[Path, str]] = []
    if config.global_memory_path.exists():
        documents.append((config.global_memory_path, GLOBAL_SCOPE))
    documents.extend((path, LOCAL_RECENT_SCOPE) for path in sorted(config.recent_dir.glob("*.md")))
    documents.extend((path, LOCAL_ARCHIVE_SCOPE) for path in sorted(config.archive_dir.glob("*/*/*.md")))
    return documents


def _migrate_document(document: MemoryDocument) -> tuple[MemoryDocument, bool, int, int]:
    changed = False
    changed_records = 0
    scanned_records = 0
    sections: dict[str, list[MemoryRecord]] = {}
    for section, records in document.sections.items():
        migrated_records: list[MemoryRecord] = []
        for record in records:
            scanned_records += 1
            migrated = _migrate_record(record)
            if migrated != record:
                changed = True
                changed_records += 1
            migrated_records.append(migrated)
        sections[section] = migrated_records
    if not changed:
        return document, False, changed_records, scanned_records
    return (
        MemoryDocument(scope=document.scope, metadata=dict(document.metadata), sections=sections),
        True,
        changed_records,
        scanned_records,
    )


def _migrate_record(record: MemoryRecord) -> MemoryRecord:
    subject = _translate_subject(record.subject)
    summary = _translate_summary(record.summary)
    rationale = _translate_rationale(record.rationale)
    next_use = _translate_next_use(record.next_use)
    scope_reason = _translate_scope_reason(record.scope_reason)
    return replace(
        record,
        subject=subject,
        summary=summary,
        rationale=rationale,
        next_use=next_use,
        scope_reason=scope_reason,
    )


def _translate_subject(value: str) -> str:
    translations = {
        "Assistant name preference": "助手名称偏好",
        "Launchd daemon smoke test": "Launchd 常驻进程冒烟测试",
        "Revisit the failing auth snapshot": "重新检查失败的 auth 快照",
        "next step": "下一步",
    }
    return translations.get(value, value)


def _translate_summary(value: str) -> str:
    if not value:
        return value
    matched = _ASSISTANT_NAME_SUMMARY_RE.match(value)
    if matched:
        return f"用户希望在后续对话中把助手称为 {matched.group('name')}。"
    matched = _NEAR_TERM_STEP_RE.match(value)
    if matched:
        step = _translate_action_phrase(matched.group("step"))
        return f"当前仓库近期下一步：{step}。"
    matched = _ZH_NEAR_TERM_STEP_RE.match(value)
    if matched:
        step = _translate_action_phrase(matched.group("step"))
        return f"当前仓库近期下一步：{step}。"
    translations = {
        "Peer clone already observed the auth snapshot failure.": "同仓库的另一个工作副本已经观察到 auth 快照失败。",
        "Retry the auth snapshot flow.": "重试 auth 快照流程。",
        "已对当前记忆库执行一次 `migrate-zh` 迁移，共更新 3 条记录；当前会保留结构字段，只将说明性字段转成中文，动作短语仍可能保留英文原句。": "已对当前记忆库执行一次 `migrate-zh` 迁移，共更新 3 条记录；当前会保留结构字段，并已将说明性字段与动作短语一并转成中文。",
    }
    return translations.get(value, value)


def _translate_rationale(value: str | None) -> str | None:
    if not value:
        return value
    if value.startswith("The user explicitly said:"):
        quoted = value.split(":", 1)[1].strip()
        return f"用户明确说过：{quoted}"
    return value


def _translate_next_use(value: str | None) -> str | None:
    if not value:
        return value
    matched = _ASSISTANT_NAME_NEXT_USE_RE.match(value)
    if matched:
        return f"后续对话中将助手称为 {matched.group('name')}。"
    if value == "Surface when resuming work in this repository as an immediate follow-up task.":
        return "恢复该仓库工作时，优先展示并继续这项任务。"
    if "仍有英文动作短语" in value:
        return "后续若继续处理记忆中文化或排查相关问题，先参考这条状态记录。"
    return value


def _translate_scope_reason(value: str) -> str:
    if not value:
        return value
    translations = {
        "This is a durable user preference that applies across repositories and future sessions, so it belongs in global memory.": "这是跨仓库、跨会话都成立的长期用户偏好，应归入全局记忆。",
        "repo-specific and near-term": "仓库内近期待办",
        "repo specific": "仓库内近期待办",
        "cross workspace preference": "跨工作区长期偏好",
    }
    return translations.get(value, value)
