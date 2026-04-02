from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any

from .archive import archive_stale_recent_documents
from .bootstrap import ensure_layout
from .config import resolve_config
from .constants import GLOBAL_SCOPE, LOCAL_RECENT_SCOPE
from .migration import migrate_records_to_zh
from .markdown_store import get_record, load_document
from .patch_applier import apply_patch_plan, current_base_revisions
from .record_store import find_record
from .search_index import SearchIndex, search_old_records
from .snapshot import build_snapshot
from .worker import run_worker_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-admin")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--memory-home", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("bootstrap")
    subparsers.add_parser("context")
    subparsers.add_parser("archive")
    subparsers.add_parser("migrate-zh")
    subparsers.add_parser("print-hooks-config")
    rebuild_parser = subparsers.add_parser("rebuild-index")
    rebuild_parser.add_argument("--json", action="store_true")

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query", nargs="?", default="")
    search_parser.add_argument("--top-k", type=int, default=8)
    search_parser.add_argument("--workspace-instance-id")
    search_parser.add_argument("--repo-id")
    search_parser.add_argument(
        "--search-scope", choices=["current_workspace", "same_repo"], default="current_workspace"
    )

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("record_id")

    worker_parser = subparsers.add_parser("worker-run-once")
    worker_parser.add_argument("--backend", default="qwen", choices=["qwen", "heuristic"])

    upsert_parser = subparsers.add_parser("upsert")
    upsert_parser.add_argument("--scope", required=True, choices=["global", "local"])
    upsert_parser.add_argument("--id")
    upsert_parser.add_argument("--type", required=True)
    upsert_parser.add_argument("--status")
    upsert_parser.add_argument("--confidence", default="medium")
    upsert_parser.add_argument("--subject", required=True)
    upsert_parser.add_argument("--summary", required=True)
    upsert_parser.add_argument("--tags", default="")
    upsert_parser.add_argument("--source-ref", action="append", default=[])
    upsert_parser.add_argument("--scope-reason", default="")
    upsert_parser.add_argument("--rationale")
    upsert_parser.add_argument("--next-use")
    upsert_parser.add_argument("--pin-until")

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("--scope", required=True, choices=["global", "local"])
    delete_parser.add_argument("record_id")
    delete_parser.add_argument("--reason", required=True)

    pin_parser = subparsers.add_parser("pin")
    pin_parser.add_argument("record_id")
    pin_parser.add_argument("--pin-until", required=True)

    args = parser.parse_args(argv)
    config = resolve_config(args.cwd, args.memory_home)
    ensure_layout(config)

    if args.command == "bootstrap":
        print(json.dumps({"memory_home": str(config.memory_home), "workspace_root": str(config.workspace_root)}))
        return 0
    if args.command == "context":
        snapshot = build_snapshot(config)
        print(snapshot.rendered_text)
        return 0
    if args.command == "archive":
        archived = archive_stale_recent_documents(config)
        _refresh_index(config)
        print(json.dumps({"archived": [str(path) for path in archived]}, indent=2))
        return 0
    if args.command == "migrate-zh":
        payload = migrate_records_to_zh(config)
        _refresh_index(config)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if args.command == "print-hooks-config":
        print(json.dumps(_hooks_config(config), indent=2))
        return 0
    if args.command == "rebuild-index":
        index = SearchIndex(config.index_db_path)
        try:
            count = index.rebuild(config)
        finally:
            index.close()
        payload = {"indexed_records": count}
        print(json.dumps(payload, indent=2 if args.json else None))
        return 0
    if args.command == "search":
        rows = search_old_records(
            config,
            workspace_instance_id=args.workspace_instance_id,
            repo_id=args.repo_id,
            query=args.query,
            top_k=args.top_k,
            search_scope=args.search_scope,
        )
        print(json.dumps(rows, indent=2))
        return 0
    if args.command == "get":
        match = find_record(config, args.record_id)
        if match is None:
            raise SystemExit(f"record not found: {args.record_id}")
        path, scope, section, record = match
        print(
            json.dumps(
                {"path": str(path), "scope": scope, "section": section, "record": record.to_dict()},
                indent=2,
            )
        )
        return 0
    if args.command == "worker-run-once":
        payload = run_worker_once(args.cwd, memory_home=args.memory_home, backend=args.backend)
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "upsert":
        patch_plan = _build_upsert_patch_plan(config, args)
        result = apply_patch_plan(config, patch_plan)
        _refresh_index(config)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "delete":
        patch_plan = _build_delete_patch_plan(config, args)
        result = apply_patch_plan(config, patch_plan)
        _refresh_index(config)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "pin":
        patch_plan = {
            "decision": "write",
            "reason": "manual pin",
            "base_revisions": current_base_revisions(config),
            "global_ops": [],
            "local_ops": [{"action": "pin", "target_id": args.record_id, "pin": {"pin_until": args.pin_until}}],
            "needs_manual_review": False,
        }
        result = apply_patch_plan(config, patch_plan)
        _refresh_index(config)
        print(json.dumps(result, indent=2))
        return 0
    raise SystemExit(f"unsupported command: {args.command}")


def _build_upsert_patch_plan(config, args) -> dict[str, Any]:
    tags = _normalize_list_argument(args.tags)
    source_refs = _normalize_list_argument(args.source_ref)
    base_revisions = current_base_revisions(config)
    record_payload = {
        "type": args.type,
        "status": args.status or ("active" if args.scope == "global" else "open"),
        "confidence": args.confidence,
        "subject": args.subject,
        "summary": args.summary,
        "tags": tags,
        "source_refs": source_refs,
        "scope_reason": args.scope_reason,
    }
    if args.rationale:
        record_payload["rationale"] = args.rationale
    if args.next_use:
        record_payload["next_use"] = args.next_use
    if args.pin_until:
        record_payload["pin_until"] = args.pin_until

    global_ops: list[dict[str, Any]] = []
    local_ops: list[dict[str, Any]] = []
    if args.id:
        if _record_exists_for_scope(config, args.scope, args.id):
            op = {"action": "update", "target_id": args.id, "record_patch": record_payload}
        else:
            op = {"action": "create", "record": {**record_payload, "id": args.id}}
    else:
        op = {"action": "create", "record": record_payload}
    if args.scope == "global":
        global_ops.append(op)
    else:
        local_ops.append(op)
    return {
        "decision": "write",
        "reason": "manual upsert",
        "base_revisions": base_revisions,
        "global_ops": global_ops,
        "local_ops": local_ops,
        "needs_manual_review": False,
    }


def _build_delete_patch_plan(config, args) -> dict[str, Any]:
    base_revisions = current_base_revisions(config)
    op = {
        "action": "delete",
        "target_id": args.record_id,
        "tombstone": {"reason": args.reason, "source_refs": []},
    }
    return {
        "decision": "write",
        "reason": "manual delete",
        "base_revisions": base_revisions,
        "global_ops": [op] if args.scope == "global" else [],
        "local_ops": [op] if args.scope == "local" else [],
        "needs_manual_review": False,
    }


def _normalize_list_argument(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _refresh_index(config) -> None:
    index = SearchIndex(config.index_db_path)
    try:
        index.rebuild(config)
    finally:
        index.close()


def _record_exists_for_scope(config, scope: str, record_id: str) -> bool:
    if scope == "global":
        if not config.global_memory_path.exists():
            return False
        document = load_document(config.global_memory_path, GLOBAL_SCOPE)
        return get_record(document, record_id) is not None
    for path in sorted(config.recent_dir.glob("*.md")):
        document = load_document(path, LOCAL_RECENT_SCOPE)
        if get_record(document, record_id) is not None:
            return True
    return False


def _hooks_config(config) -> dict[str, Any]:
    hook_path = str((Path(__file__).resolve().parents[1] / "bin" / "memory-hook"))
    command_prefix = f"CODEX_MEMORY_HOME={shlex.quote(str(config.memory_home))} "
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix}{shlex.quote(hook_path)} session-start",
                            "timeout": 10,
                            "statusMessage": "Prewarming memory",
                        }
                    ],
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix}{shlex.quote(hook_path)} user-prompt-submit",
                            "timeout": 15,
                            "statusMessage": "Loading memory",
                        }
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{command_prefix}{shlex.quote(hook_path)} stop",
                            "timeout": 8,
                            "statusMessage": "Recording memory candidate",
                        }
                    ]
                }
            ],
        }
    }
