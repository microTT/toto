from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import MemoryConfig, resolve_config
from .constants import GLOBAL_SCOPE, LOCAL_ARCHIVE_SCOPE, LOCAL_RECENT_SCOPE
from .markdown_store import load_document
from .models import MemoryDocument, MemoryRecord
from .snapshot import build_snapshot
from .workspace_store import (
    iter_peer_memory_configs,
    iter_scoped_archive_documents,
    iter_scoped_recent_documents,
)

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-web")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--memory-home", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=59112)
    args = parser.parse_args(argv)

    config = resolve_config(args.cwd, args.memory_home)
    server = create_server(config, host=args.host, port=args.port)
    print(
        json.dumps(
            {
                "ok": True,
                "url": f"http://{args.host}:{args.port}",
                "workspace_root": str(config.workspace_root),
                "memory_home": str(config.memory_home),
            },
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def create_server(config: MemoryConfig, *, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(config))
    server.daemon_threads = True
    return server


def make_handler(config: MemoryConfig):
    class MemoryWebHandler(BaseHTTPRequestHandler):
        server_version = "memory-web/0.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/health":
                    self._send_json(HTTPStatus.OK, build_health_payload(config))
                    return
                if parsed.path == "/api/workspaces":
                    self._send_json(HTTPStatus.OK, build_workspace_index_payload(config))
                    return
                if parsed.path.startswith("/api/workspaces/"):
                    remainder = parsed.path[len("/api/workspaces/") :]
                    segments = [segment for segment in remainder.split("/") if segment]
                    if len(segments) == 1:
                        target = resolve_workspace_config(config, segments[0])
                        if target is None:
                            self._send_json(HTTPStatus.NOT_FOUND, {"error": "workspace not found"})
                            return
                        self._send_json(HTTPStatus.OK, build_workspace_detail_payload(target, current_config=config))
                        return
                    if len(segments) == 3 and segments[1] == "records":
                        target = resolve_workspace_config(config, segments[0])
                        if target is None:
                            self._send_json(HTTPStatus.NOT_FOUND, {"error": "workspace not found"})
                            return
                        record = find_record_view(target, segments[2])
                        if record is None:
                            self._send_json(HTTPStatus.NOT_FOUND, {"error": "record not found"})
                            return
                        self._send_json(HTTPStatus.OK, record)
                        return

                self._serve_static(parsed.path)
            except Exception as error:  # pragma: no cover - defensive HTTP surface
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(error)},
                )

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, raw_path: str) -> None:
            requested = "index.html" if raw_path in {"", "/"} else raw_path.lstrip("/")
            resolved = (WEB_ROOT / requested).resolve()
            if not _is_safe_child(WEB_ROOT, resolved) or not resolved.is_file():
                self._send_bytes(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain")
                return
            content_type = {
                ".html": "text/html",
                ".css": "text/css",
                ".js": "application/javascript",
            }.get(resolved.suffix, "application/octet-stream")
            self._send_bytes(HTTPStatus.OK, resolved.read_bytes(), content_type)

    return MemoryWebHandler


def build_health_payload(config: MemoryConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "workspace_root": str(config.workspace_root),
        "workspace_instance_id": config.workspace_instance_id,
        "repo_id": config.repo_id,
        "memory_home": str(config.memory_home),
    }


def build_workspace_index_payload(config: MemoryConfig) -> dict[str, Any]:
    workspaces = [build_workspace_summary(item, current_config=config) for item in iter_peer_memory_configs(config)]
    workspaces.sort(key=_workspace_sort_key, reverse=True)
    return {
        "current_workspace_id": config.workspace_instance_id,
        "memory_home_parent": str(config.memory_home.parent),
        "workspace_count": len(workspaces),
        "workspaces": workspaces,
    }


def build_workspace_detail_payload(
    config: MemoryConfig,
    *,
    current_config: MemoryConfig | None = None,
) -> dict[str, Any]:
    summary = build_workspace_summary(config, current_config=current_config)
    global_document = load_document(config.global_memory_path, GLOBAL_SCOPE)
    recent_documents = list(iter_scoped_recent_documents(config))
    archive_documents = list(iter_scoped_archive_documents(config))
    records = flatten_workspace_records(
        config,
        global_document=global_document,
        recent_documents=recent_documents,
        archive_documents=archive_documents,
    )
    snapshot = load_latest_runtime_snapshot(config)
    if snapshot is None:
        snapshot = {
            "source": "rebuilt",
            "session_id": None,
            **build_snapshot(config).to_dict(),
        }
    return {
        "workspace": summary,
        "snapshot": snapshot,
        "documents": {
            "global": serialize_document_summary(config.global_memory_path, global_document, GLOBAL_SCOPE),
            "recent": [
                serialize_document_summary(path, document, LOCAL_RECENT_SCOPE)
                for path, document in recent_documents
            ],
            "archive": [
                serialize_document_summary(path, document, LOCAL_ARCHIVE_SCOPE)
                for path, document in archive_documents
            ],
        },
        "records": records,
    }


def resolve_workspace_config(base_config: MemoryConfig, workspace_instance_id: str) -> MemoryConfig | None:
    for candidate in iter_peer_memory_configs(base_config):
        if candidate.workspace_instance_id == workspace_instance_id:
            return candidate
    return None


def find_record_view(config: MemoryConfig, record_id: str) -> dict[str, Any] | None:
    detail = build_workspace_detail_payload(config)
    for record in detail["records"]:
        if record["id"] == record_id:
            return record
    return None


def build_workspace_summary(
    config: MemoryConfig,
    *,
    current_config: MemoryConfig | None = None,
) -> dict[str, Any]:
    global_document = load_document(config.global_memory_path, GLOBAL_SCOPE)
    recent_documents = [document for _, document in iter_scoped_recent_documents(config)]
    archive_documents = [document for _, document in iter_scoped_archive_documents(config)]
    latest_snapshot = load_latest_runtime_snapshot(config)
    counts = {
        "global": sum(len(records) for records in global_document.sections.values()),
        "recent": sum(sum(len(records) for records in document.sections.values()) for document in recent_documents),
        "archive": sum(sum(len(records) for records in document.sections.values()) for document in archive_documents),
    }
    latest_record_at = max(
        (
            record.updated_at or record.created_at or ""
            for document in [global_document, *recent_documents, *archive_documents]
            for records in document.sections.values()
            for record in records
        ),
        default="",
    )
    latest_snapshot_at = ""
    if latest_snapshot is not None:
        latest_snapshot_at = str(latest_snapshot.get("built_at") or "")
    label = config.workspace_root.name or str(config.workspace_root)
    return {
        "workspace_instance_id": config.workspace_instance_id,
        "repo_id": config.repo_id,
        "workspace_root": str(config.workspace_root),
        "memory_home": str(config.memory_home),
        "label": label,
        "is_current": current_config is not None and config.workspace_instance_id == current_config.workspace_instance_id,
        "counts": counts,
        "latest_record_at": latest_record_at,
        "latest_snapshot_at": latest_snapshot_at,
    }


def flatten_workspace_records(
    config: MemoryConfig,
    *,
    global_document: MemoryDocument,
    recent_documents: list[tuple[Path, MemoryDocument]],
    archive_documents: list[tuple[Path, MemoryDocument]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    records.extend(
        _flatten_document_records(
            config,
            config.global_memory_path,
            global_document,
            scope=GLOBAL_SCOPE,
        )
    )
    for path, document in recent_documents:
        records.extend(_flatten_document_records(config, path, document, scope=LOCAL_RECENT_SCOPE))
    for path, document in archive_documents:
        records.extend(_flatten_document_records(config, path, document, scope=LOCAL_ARCHIVE_SCOPE))
    records.sort(key=_record_sort_key, reverse=True)
    return records


def serialize_document_summary(path: Path, document: MemoryDocument, scope: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "scope": scope,
        "metadata": dict(document.metadata),
        "section_counts": {section: len(records) for section, records in document.sections.items()},
        "record_count": sum(len(records) for records in document.sections.values()),
    }


def load_latest_runtime_snapshot(config: MemoryConfig) -> dict[str, Any] | None:
    best: tuple[str, dict[str, Any], Path] | None = None
    for path in sorted(config.runtime_dir.glob("session_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        built_at = str(payload.get("built_at") or "")
        if best is None or built_at > best[0]:
            best = (built_at, payload, path)
    if best is None:
        return None
    _, payload, path = best
    session_id = path.stem.removeprefix("session_")
    return {
        "source": "runtime",
        "path": str(path),
        "session_id": session_id,
        **payload,
    }


def _flatten_document_records(
    config: MemoryConfig,
    path: Path,
    document: MemoryDocument,
    *,
    scope: str,
) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for section, records in document.sections.items():
        for record in records:
            flattened.append(
                serialize_record(
                    config,
                    record,
                    path=path,
                    scope=scope,
                    section=section,
                    metadata=document.metadata,
                )
            )
    return flattened


def serialize_record(
    config: MemoryConfig,
    record: MemoryRecord,
    *,
    path: Path,
    scope: str,
    section: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    payload = record.to_dict()
    payload.update(
        {
            "path": str(path),
            "scope": scope,
            "section": section,
            "repo_id": config.repo_id,
            "workspace_instance_id": config.workspace_instance_id,
            "workspace_root": str(config.workspace_root),
            "document_date": metadata.get("date"),
            "document_revision": metadata.get("revision"),
        }
    )
    return payload


def _workspace_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("latest_record_at") or ""),
        str(item.get("latest_snapshot_at") or ""),
        str(item.get("workspace_root") or ""),
    )


def _record_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("updated_at") or ""),
        str(item.get("created_at") or ""),
        str(item.get("id") or ""),
    )


def _is_safe_child(parent: Path, child: Path) -> bool:
    if child == parent:
        return True
    return parent in child.parents


if __name__ == "__main__":
    raise SystemExit(main())
