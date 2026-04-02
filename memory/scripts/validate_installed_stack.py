#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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

    checks: list[dict[str, object]] = []
    checks.append(_check_installed_config(hook_path, mcp_path, memory_home))
    checks.append(_check_mcp_stdio(mcp_path, workspace, memory_home))
    checks.append(
        _run_codex_exec(
            workspace,
            "Remember next step: revisit the failing auth snapshot. This is repo-specific and near-term. Reply with exactly ack.",
            expected_last_message="ack",
            expected_hooks=["SessionStart Completed", "UserPromptSubmit Completed", "Stop Completed"],
        )
    )
    checks.append(_run_worker(memoryd_path, workspace, memory_home))
    checks.append(
        _run_codex_exec(
            workspace,
            "What is the remembered next step for this repository? Reply with exactly the task phrase and nothing else.",
            expected_last_message=args.expected_task,
            expected_hooks=["SessionStart Completed", "UserPromptSubmit Completed", "Stop Completed"],
        )
    )
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
        [str(mcp_path), "--cwd", str(workspace), "--memory-home", str(memory_home)],
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
    context_text = responses[2]["result"]["content"][0]["text"]
    if "rendered_text" not in context_text:
        raise AssertionError("memory.get_context did not return snapshot payload")
    return {"name": "mcp_stdio", "ok": True}


def _run_codex_exec(
    workspace: Path,
    prompt: str,
    *,
    expected_last_message: str,
    expected_hooks: list[str],
) -> dict[str, object]:
    last_message_path = Path("/tmp") / f"memory-live-{abs(hash(prompt))}.txt"
    completed = subprocess.run(
        [
            "codex",
            "exec",
            "-C",
            str(workspace),
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            str(last_message_path),
            prompt,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"codex exec failed: {completed.stdout}")
    output = completed.stdout
    for hook_marker in expected_hooks:
        if hook_marker not in output:
            raise AssertionError(f"missing hook marker {hook_marker!r} in codex output")
    last_message = last_message_path.read_text(encoding="utf-8").strip()
    if _normalize_last_message(last_message) != _normalize_last_message(expected_last_message):
        raise AssertionError(f"unexpected last message: {last_message!r} != {expected_last_message!r}")
    return {"name": f"codex_exec:{expected_last_message}", "ok": True}


def _normalize_last_message(value: str) -> str:
    return value.strip().rstrip(".。!！").casefold()


def _run_worker(memoryd_path: Path, workspace: Path, memory_home: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(memoryd_path),
            "run-once",
            "--cwd",
            str(workspace),
            "--memory-home",
            str(memory_home),
            "--backend",
            "codex",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"memoryd run-once failed: {completed.stdout}")
    payload = json.loads(completed.stdout)
    if payload is None:
        return {
            "name": "memoryd_codex",
            "ok": True,
            "payload": None,
            "note": "no pending job (queue may already be drained by daemon)",
        }
    if payload.get("error"):
        raise AssertionError(f"memoryd worker returned error payload: {payload['error']}")
    return {"name": "memoryd_codex", "ok": True, "payload": payload}


if __name__ == "__main__":
    raise SystemExit(run())
