#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="validate_installed_stack")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--memory-home", required=True)
    parser.add_argument("--expected-task", default="重新检查失败的 auth 快照")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    memory_home = Path(args.memory_home).resolve()
    repo_root = Path(__file__).resolve().parents[2]
    hook_path = repo_root / "memory" / "bin" / "memory-hook"
    mcp_path = repo_root / "memory" / "bin" / "memory-mcp"
    memoryd_path = repo_root / "memory" / "bin" / "memoryd"
    admin_path = repo_root / "memory" / "bin" / "memory-admin"
    session_id = f"memory-live-check-session-{int(time.time())}"
    env = _hook_env(memory_home)

    checks: list[dict[str, object]] = []
    checks.append(_check_installed_config(hook_path, mcp_path, memory_home))
    checks.append(_check_mcp_stdio(mcp_path, workspace, memory_home))
    checks.append(_run_hook(hook_path, "session-start", _base_hook_payload(session_id, workspace, "turn-0"), env=env))
    checks.append(
        _run_user_prompt_submit(
            hook_path,
            workspace,
            session_id=session_id,
            turn_id="turn-1",
            prompt="请加载当前仓库记忆。",
            env=env,
            expected_substrings=["记忆已加载", "工作区近期记忆"],
            name="hook:user-prompt-submit:prewarm",
        )
    )
    checks.append(
        _run_hook(
            hook_path,
            "stop",
            {
                **_base_hook_payload(session_id, workspace, "turn-2"),
                "user_message_delta": "记住下一步：重新检查失败的 auth 快照。",
                "assistant_message_delta": "已记录。",
            },
            env=env,
        )
    )
    checks.append(_run_worker(memoryd_path, workspace, memory_home, target_session_id=session_id))
    checks.append(
        _run_user_prompt_submit(
            hook_path,
            workspace,
            session_id=session_id,
            turn_id="turn-3",
            prompt="当前仓库记住的下一步是什么？",
            env=env,
            expected_substrings=[args.expected_task],
            name="hook:user-prompt-submit:post-summary",
        )
    )
    checks.append(_check_context(admin_path, workspace, memory_home, expected_substring=args.expected_task))
    print(json.dumps({"ok": True, "checks": checks}, indent=2, ensure_ascii=False))
    return 0


def _check_installed_config(hook_path: Path, mcp_path: Path, memory_home: Path) -> dict[str, object]:
    hooks_path = Path.home() / ".codex" / "hooks.json"
    config_path = Path.home() / ".codex" / "config.toml"
    hooks_payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks_text = json.dumps(hooks_payload, ensure_ascii=False)
    config_text = config_path.read_text(encoding="utf-8")
    if str(hook_path) not in hooks_text:
        raise AssertionError(f"{hooks_path} does not reference {hook_path}")
    if str(memory_home) not in hooks_text:
        raise AssertionError(f"{hooks_path} does not pin CODEX_MEMORY_HOME={memory_home}")
    if str(mcp_path) not in config_text:
        raise AssertionError(f"{config_path} does not reference {mcp_path}")
    if str(memory_home) not in config_text:
        raise AssertionError(f"{config_path} does not pass --memory-home {memory_home}")
    if '"--allow-writes"' not in config_text:
        raise AssertionError(f"{config_path} does not enable --allow-writes for memory-local MCP")
    return {"name": "installed_config", "ok": True}


def _check_mcp_stdio(mcp_path: Path, workspace: Path, memory_home: Path) -> dict[str, object]:
    request_stream = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "memory-live-check", "version": "1.0"},
                    },
                }
            ),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "memory.get_context", "arguments": {}},
                }
            ),
            "",
        ]
    )
    completed = subprocess.run(
        [str(mcp_path), "--cwd", str(workspace), "--memory-home", str(memory_home), "--allow-writes"],
        input=request_stream,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"memory-mcp failed: {completed.stderr.strip()}")
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    if "memory.get_context" not in tool_names:
        raise AssertionError("memory.get_context missing from tools/list")
    if "memory.delete" not in tool_names:
        raise AssertionError("memory.delete missing from tools/list")
    context_text = responses[2]["result"]["content"][0]["text"]
    if "rendered_text" not in context_text:
        raise AssertionError("memory.get_context did not return snapshot payload")
    return {"name": "mcp_stdio", "ok": True}


def _run_hook(
    hook_path: Path,
    command: str,
    payload: dict[str, object],
    *,
    env: dict[str, str],
) -> dict[str, object]:
    completed = subprocess.run(
        [str(hook_path), command],
        input=json.dumps(payload, ensure_ascii=False),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"memory-hook {command} failed: {completed.stderr.strip()}")
    return {"name": f"hook:{command}", "ok": True}


def _run_user_prompt_submit(
    hook_path: Path,
    workspace: Path,
    *,
    session_id: str,
    turn_id: str,
    prompt: str,
    env: dict[str, str],
    expected_substrings: list[str],
    name: str,
) -> dict[str, object]:
    completed = subprocess.run(
        [str(hook_path), "user-prompt-submit"],
        input=json.dumps(
            {
                **_base_hook_payload(session_id, workspace, turn_id),
                "user_message_delta": prompt,
            },
            ensure_ascii=False,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"memory-hook user-prompt-submit failed: {completed.stderr.strip()}")
    try:
        payload = json.loads(completed.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
    except Exception as exc:
        raise AssertionError(f"user-prompt-submit returned invalid payload: {completed.stdout}") from exc
    for expected in expected_substrings:
        if expected not in context:
            raise AssertionError(f"user-prompt-submit context missing {expected!r}: {context}")
    return {"name": name, "ok": True}


def _run_worker(
    memoryd_path: Path,
    workspace: Path,
    memory_home: Path,
    *,
    target_session_id: str,
    max_iterations: int = 8,
) -> dict[str, object]:
    seen_payloads: list[dict[str, object] | None] = []
    for _ in range(max_iterations):
        completed = subprocess.run(
            [
                str(memoryd_path),
                "run-once",
                "--cwd",
                str(workspace),
                "--memory-home",
                str(memory_home),
                "--backend",
                "qwen",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(f"memoryd run-once failed: {completed.stdout}")
        payload = json.loads(completed.stdout)
        seen_payloads.append(payload)
        if payload is None:
            break
        if payload.get("error"):
            raise AssertionError(f"memoryd worker returned error payload: {payload['error']}")
        job = payload.get("job") or {}
        if job.get("session_id") == target_session_id:
            return {"name": "memoryd_qwen", "ok": True, "payload": payload}
    raise AssertionError(
        f"memoryd did not process validation session {target_session_id!r}; seen payloads: "
        f"{json.dumps(seen_payloads, ensure_ascii=False)}"
    )


def _check_context(
    admin_path: Path,
    workspace: Path,
    memory_home: Path,
    *,
    expected_substring: str,
) -> dict[str, object]:
    completed = subprocess.run(
        [str(admin_path), "--cwd", str(workspace), "--memory-home", str(memory_home), "context"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"memory-admin context failed: {completed.stderr.strip()}")
    context = completed.stdout
    if expected_substring not in context:
        raise AssertionError(f"context output missing {expected_substring!r}: {context}")
    return {"name": "memory_admin_context", "ok": True}


def _base_hook_payload(session_id: str, workspace: Path, turn_id: str) -> dict[str, object]:
    return {"session_id": session_id, "turn_id": turn_id, "cwd": str(workspace)}


def _hook_env(memory_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_MEMORY_HOME"] = str(memory_home)
    return env


if __name__ == "__main__":
    raise SystemExit(run())
