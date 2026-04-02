from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .config import MemoryConfig
from .constants import GLOBAL_SCOPE, LOCAL_ARCHIVE_SCOPE, LOCAL_RECENT_SCOPE
from .markdown_store import get_record, load_document
from .models import MemoryDocument, MemoryRecord


def iter_documents(config: MemoryConfig) -> Iterator[tuple[Path, str, MemoryDocument]]:
    if config.global_memory_path.exists():
        yield config.global_memory_path, GLOBAL_SCOPE, load_document(config.global_memory_path, GLOBAL_SCOPE)
    for path in sorted(config.recent_dir.glob("*.md")):
        yield path, LOCAL_RECENT_SCOPE, load_document(path, LOCAL_RECENT_SCOPE)
    for path in sorted(config.archive_dir.glob("*/*/*.md")):
        yield path, LOCAL_ARCHIVE_SCOPE, load_document(path, LOCAL_ARCHIVE_SCOPE)


def find_record(config: MemoryConfig, record_id: str) -> tuple[Path, str, str, MemoryRecord] | None:
    for path, scope, document in iter_documents(config):
        match = get_record(document, record_id)
        if match is not None:
            section, record = match
            return path, scope, section, record
    return None
