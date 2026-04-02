from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .admin import _build_delete_patch_plan, _build_upsert_patch_plan
from .bootstrap import ensure_layout
from .config import resolve_config
from .patch_applier import apply_patch_plan
from .record_store import find_record
from .search_index import SearchIndex, search_old_records
from .snapshot import build_snapshot


READ_ONLY_TOOLS = [
    {
        "name": "memory.search_old",
        "description": "Search archived workspace memory records.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_instance_id": {"type": "string"},
                "repo_id": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 8},
                "search_scope": {"type": "string", "enum": ["current_workspace", "same_repo"]},
            },
            "required": [],
        },
    },
    {
        "name": "memory.get",
        "description": "Fetch a memory record by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "memory.get_context",
        "description": "Return the currently auto-loaded global and recent memory snapshot.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-mcp")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--memory-home", default=None)
    parser.add_argument("--allow-writes", action="store_true")
    args = parser.parse_args(argv)
    config = resolve_config(args.cwd, args.memory_home)
    ensure_layout(config)
    tools = list(READ_ONLY_TOOLS)
    if args.allow_writes:
        tools.extend(
            [
                {
                    "name": "memory.upsert",
                    "description": "Create or update a memory record.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "explicit_user_request": {"type": "boolean"},
                            "scope": {"type": "string", "enum": ["global", "local"]},
                            "id": {"type": "string"},
                            "type": {"type": "string"},
                            "status": {"type": "string"},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            "subject": {"type": "string"},
                            "summary": {"type": "string"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "source_ref": {"type": "array", "items": {"type": "string"}},
                            "scope_reason": {"type": "string"},
                            "rationale": {"type": "string"},
                            "next_use": {"type": "string"},
                            "pin_until": {"type": "string"}
                        },
                        "required": ["explicit_user_request", "scope", "type", "subject", "summary"]
                    },
                },
                {
                    "name": "memory.delete",
                    "description": "Soft delete a memory record.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "explicit_user_request": {"type": "boolean"},
                            "scope": {"type": "string", "enum": ["global", "local"]},
                            "record_id": {"type": "string"},
                            "reason": {"type": "string"}
                        },
                        "required": ["explicit_user_request", "scope", "record_id", "reason"]
                    },
                },
                {
                    "name": "memory.rebuild_index",
                    "description": "Rebuild the memory search index.",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                },
            ]
        )
    for line in sys.stdin:
        request = json.loads(line)
        response = _handle_request(config, request, allow_writes=args.allow_writes, tools=tools)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


def _handle_request(
    config, request: dict[str, Any], *, allow_writes: bool, tools: list[dict[str, Any]]
) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "memory-mcp", "version": "0.1.0"}},
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": tools}}
    if method == "tools/call":
        name = request.get("params", {}).get("name")
        arguments = request.get("params", {}).get("arguments", {})
        result = _call_tool(config, name, arguments, allow_writes=allow_writes)
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True)}]},
        }
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {"code": -32601, "message": f"unsupported method: {method}"},
    }


def _call_tool(config, name: str, arguments: dict[str, Any], *, allow_writes: bool) -> Any:
    if name == "memory.search_old":
        return search_old_records(
            config,
            workspace_instance_id=arguments.get("workspace_instance_id"),
            repo_id=arguments.get("repo_id"),
            query=arguments.get("query", ""),
            top_k=int(arguments.get("top_k", 8)),
            search_scope=arguments.get("search_scope", "current_workspace"),
        )
    if name == "memory.get":
        return _record_result(config, arguments["record_id"])
    if name == "memory.get_context":
        snapshot = build_snapshot(config)
        return snapshot.to_dict()
    if not allow_writes:
        raise RuntimeError("write tools are disabled for this server")
    if name == "memory.rebuild_index":
        index = SearchIndex(config.index_db_path)
        try:
            return {"indexed_records": index.rebuild(config)}
        finally:
            index.close()
    if name == "memory.upsert":
        _require_explicit_user_request(arguments)
        args = argparse.Namespace(**arguments)
        patch_plan = _build_upsert_patch_plan(config, args)
        return apply_patch_plan(config, patch_plan)
    if name == "memory.delete":
        _require_explicit_user_request(arguments)
        args = argparse.Namespace(**arguments)
        patch_plan = _build_delete_patch_plan(config, args)
        return apply_patch_plan(config, patch_plan)
    raise RuntimeError(f"unsupported tool: {name}")


def _record_result(config, record_id: str) -> dict[str, Any]:
    match = find_record(config, record_id)
    if match is None:
        raise RuntimeError(f"record not found: {record_id}")
    path, scope, section, record = match
    return {"path": str(path), "scope": scope, "section": section, "record": record.to_dict()}


def _require_explicit_user_request(arguments: dict[str, Any]) -> None:
    if arguments.get("explicit_user_request") is not True:
        raise RuntimeError("memory write tools require explicit_user_request=true")
