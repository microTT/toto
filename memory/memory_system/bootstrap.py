from __future__ import annotations

from pathlib import Path

from .config import MemoryConfig
from .constants import GLOBAL_SCOPE
from .markdown_store import empty_document, save_document


def ensure_layout(config: MemoryConfig) -> None:
    for path in (
        config.control_dir,
        config.jobs_dir,
        config.global_dir / "audit",
        config.recent_dir,
        config.archive_dir,
        config.runtime_dir,
        config.workspace_audit_path.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)
    if not config.global_memory_path.exists():
        save_document(config.global_memory_path, empty_document(GLOBAL_SCOPE, path=config.global_memory_path))
