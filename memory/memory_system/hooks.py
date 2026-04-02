from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bootstrap import ensure_layout
from .config import MemoryConfig, resolve_config
from .constants import DEFAULT_PROMPT_VERSION
from .snapshot import build_snapshot, compute_source_fingerprint
from .state_db import StateDB
from .utils import isoformat, read_json_file, sha256_text, write_json_file


@dataclass(slots=True)
class HookEvent:
    session_id: str
    turn_id: str
    cwd: str
    transcript_path: str | None
    user_message_delta: str | None
    assistant_message_delta: str | None
    raw_payload: dict[str, Any]


def run_hook(command: str, payload: dict[str, Any] | None = None) -> str:
    event = parse_hook_event(payload or _read_stdin_payload())
    config = resolve_config(event.cwd)
    ensure_layout(config)
    state = StateDB(config.state_db_path)
    try:
        if command == "session-start":
            return handle_session_start(config, state, event)
        if command == "user-prompt-submit":
            return handle_user_prompt_submit(config, state, event)
        if command == "stop":
            return handle_stop(config, state, event)
        raise SystemExit(f"unsupported memory hook command: {command}")
    finally:
        state.close()


def parse_hook_event(payload: dict[str, Any]) -> HookEvent:
    session_id = str(
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("session", {}).get("id")
        or "unknown-session"
    )
    turn_id = str(payload.get("turn_id") or payload.get("turnId") or payload.get("event_id") or "turn-0")
    cwd = str(payload.get("cwd") or payload.get("workspace_root") or payload.get("workspaceRoot") or Path.cwd())
    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath")
    user_message = (
        payload.get("user_message_delta")
        or payload.get("last_user_message")
        or payload.get("user_prompt")
        or payload.get("prompt")
    )
    assistant_message = payload.get("assistant_message_delta") or payload.get("last_assistant_message")
    return HookEvent(
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        transcript_path=str(transcript_path) if transcript_path else None,
        user_message_delta=user_message,
        assistant_message_delta=assistant_message,
        raw_payload=payload,
    )


def handle_session_start(config: MemoryConfig, state: StateDB, event: HookEvent) -> str:
    snapshot = build_snapshot(config)
    snapshot_path = config.session_snapshot_path(event.session_id)
    write_json_file(snapshot_path, snapshot.to_dict())
    state.save_session_snapshot(
        session_id=event.session_id,
        repo_id=config.repo_id,
        workspace_instance_id=config.workspace_instance_id,
        workspace_root=str(config.workspace_root),
        snapshot_revision=snapshot.revision,
        snapshot_json=snapshot.to_dict(),
        built_at=snapshot.built_at,
    )
    return ""


def handle_user_prompt_submit(config: MemoryConfig, state: StateDB, event: HookEvent) -> str:
    snapshot_path = config.session_snapshot_path(event.session_id)
    cached_payload = read_json_file(snapshot_path)
    current_fingerprint = compute_source_fingerprint(config)
    if cached_payload and cached_payload.get("source_fingerprint") == current_fingerprint:
        rendered = str(cached_payload["rendered_text"])
        snapshot_revision = str(cached_payload["revision"])
    else:
        snapshot = build_snapshot(config)
        write_json_file(snapshot_path, snapshot.to_dict())
        state.save_session_snapshot(
            session_id=event.session_id,
            repo_id=config.repo_id,
            workspace_instance_id=config.workspace_instance_id,
            workspace_root=str(config.workspace_root),
            snapshot_revision=snapshot.revision,
            snapshot_json=snapshot.to_dict(),
            built_at=snapshot.built_at,
        )
        rendered = snapshot.rendered_text
        snapshot_revision = snapshot.revision
    state.append_event(
        session_id=event.session_id,
        turn_id=event.turn_id,
        event_name="UserPromptSubmit",
        event_time=isoformat(),
        cwd=event.cwd,
        transcript_path=event.transcript_path,
        user_message_delta=event.user_message_delta,
        assistant_message_delta=None,
        summary_cursor_before=None,
        summary_cursor_after=snapshot_revision,
        payload=event.raw_payload,
    )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": rendered,
            }
        },
        ensure_ascii=False,
    )


def handle_stop(config: MemoryConfig, state: StateDB, event: HookEvent) -> str:
    cursor = state.get_summary_cursor(event.session_id)
    event_id = state.append_event(
        session_id=event.session_id,
        turn_id=event.turn_id,
        event_name="Stop",
        event_time=isoformat(),
        cwd=event.cwd,
        transcript_path=event.transcript_path,
        user_message_delta=event.user_message_delta,
        assistant_message_delta=event.assistant_message_delta,
        summary_cursor_before=str(cursor) if cursor else None,
        summary_cursor_after=None,
        payload=event.raw_payload,
    )
    delta_events = [dict(row) for row in state.get_events_since(event.session_id, cursor)]
    if not _should_queue_summary(delta_events):
        return ""
    job_key = sha256_text(
        "|".join(
            [
                event.session_id,
                event.transcript_path or "",
                str(cursor),
                str(event_id),
                DEFAULT_PROMPT_VERSION,
            ]
        )
    )
    state.upsert_summary_job(
        job_key=job_key,
        session_id=event.session_id,
        repo_id=config.repo_id,
        workspace_instance_id=config.workspace_instance_id,
        workspace_root=str(config.workspace_root),
        transcript_path=event.transcript_path,
        start_event_id=cursor or None,
        end_event_id=event_id,
        prompt_version=DEFAULT_PROMPT_VERSION,
        reason="threshold met",
        payload={
            "event_count": len(delta_events),
            "char_count": sum(
                len((row.get("user_message_delta") or "")) + len((row.get("assistant_message_delta") or ""))
                for row in delta_events
            ),
        },
    )
    return ""


def _should_queue_summary(delta_events: list[dict[str, Any]]) -> bool:
    if not delta_events:
        return False
    event_count = len(delta_events)
    char_count = sum(
        len((row.get("user_message_delta") or "")) + len((row.get("assistant_message_delta") or ""))
        for row in delta_events
    )
    combined = "\n".join(
        filter(
            None,
            [row.get("user_message_delta", "") for row in delta_events]
            + [row.get("assistant_message_delta", "") for row in delta_events],
        )
    ).lower()
    explicit_terms = ("记住", "remember", "forget", "update memory", "更新记忆")
    inference_terms = ("决定", "偏好", "约束", "todo", "下次继续", "失败结论", "next step", "failed")
    if any(term in combined for term in explicit_terms):
        return True
    if event_count >= 4:
        return True
    if char_count >= 1200:
        return True
    return any(term in combined for term in inference_terms)


def _read_stdin_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    return json.loads(raw)
