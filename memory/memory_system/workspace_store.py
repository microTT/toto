from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

from .config import MemoryConfig, compute_workspace_identity
from .constants import LOCAL_ARCHIVE_SCOPE, LOCAL_RECENT_SCOPE
from .markdown_store import load_document
from .models import MemoryDocument


def build_config_for_workspace(workspace_root: str | Path, memory_home: str | Path) -> MemoryConfig:
    workspace_path = Path(workspace_root).expanduser().resolve()
    memory_home_path = Path(memory_home).expanduser().resolve()
    identity = compute_workspace_identity(workspace_path)
    return MemoryConfig(
        memory_home=memory_home_path,
        workspace_root=identity.workspace_root,
        cwd=identity.workspace_root,
        repo_id=identity.repo_id,
        workspace_instance_id=identity.workspace_instance_id,
    )


def document_repo_id(document: MemoryDocument) -> str | None:
    value = document.metadata.get("repo_id")
    return str(value) if value else None


def document_workspace_instance_id(document: MemoryDocument) -> str | None:
    value = document.metadata.get("workspace_instance_id")
    return str(value) if value else None


def document_workspace_root(document: MemoryDocument) -> Path | None:
    value = document.metadata.get("workspace_root")
    if not value:
        return None
    return Path(str(value)).expanduser().resolve()


def document_matches_config(document: MemoryDocument, config: MemoryConfig) -> bool:
    repo_id = document_repo_id(document)
    if repo_id and repo_id != config.repo_id:
        return False
    workspace_instance_id = document_workspace_instance_id(document)
    if workspace_instance_id and workspace_instance_id != config.workspace_instance_id:
        return False
    workspace_root = document_workspace_root(document)
    if workspace_root is not None and workspace_root != config.workspace_root:
        return False
    return True


def iter_scoped_recent_documents(config: MemoryConfig) -> Iterator[tuple[Path, MemoryDocument]]:
    for path in sorted(config.recent_dir.glob("*.md")):
        document = load_document(path, LOCAL_RECENT_SCOPE)
        if document_matches_config(document, config):
            yield path, document


def iter_scoped_archive_documents(config: MemoryConfig) -> Iterator[tuple[Path, MemoryDocument]]:
    for path in sorted(config.archive_dir.glob("*/*/*.md")):
        document = load_document(path, LOCAL_ARCHIVE_SCOPE)
        if document_matches_config(document, config):
            yield path, document


def discover_workspace_root(memory_home: str | Path) -> Path | None:
    memory_home_path = Path(memory_home).expanduser().resolve()
    state_db_path = memory_home_path / "control" / "state.sqlite"
    if state_db_path.exists():
        root = _discover_workspace_root_from_state_db(state_db_path)
        if root is not None:
            return root
    for path in sorted((memory_home_path / "workspace" / "recent").glob("*.md"), reverse=True):
        root = document_workspace_root(load_document(path, LOCAL_RECENT_SCOPE))
        if root is not None:
            return root
    for path in sorted((memory_home_path / "workspace" / "archive").glob("*/*/*.md"), reverse=True):
        root = document_workspace_root(load_document(path, LOCAL_ARCHIVE_SCOPE))
        if root is not None:
            return root
    return None


def iter_peer_memory_configs(config: MemoryConfig) -> Iterator[MemoryConfig]:
    seen_homes: set[Path] = set()
    current_home = config.memory_home.resolve()
    seen_homes.add(current_home)
    yield config

    parent = current_home.parent
    if not parent.exists():
        return
    for home in sorted(parent.glob("wsi_*")):
        resolved_home = home.resolve()
        if resolved_home in seen_homes or not resolved_home.is_dir():
            continue
        workspace_root = discover_workspace_root(resolved_home)
        if workspace_root is None:
            continue
        seen_homes.add(resolved_home)
        yield build_config_for_workspace(workspace_root, resolved_home)


def _discover_workspace_root_from_state_db(path: Path) -> Path | None:
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        for sql in (
            "SELECT workspace_root FROM summary_jobs ORDER BY updated_at DESC, id DESC LIMIT 1",
            "SELECT workspace_root FROM session_snapshots ORDER BY built_at DESC LIMIT 1",
        ):
            row = conn.execute(sql).fetchone()
            if row is not None and row["workspace_root"]:
                return Path(str(row["workspace_root"])).expanduser().resolve()
    finally:
        conn.close()
    return None
