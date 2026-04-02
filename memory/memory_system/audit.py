from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import ensure_parent


def append_audit_event(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
