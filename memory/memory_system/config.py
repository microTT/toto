from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_TOKEN_BUDGET
from .utils import sha256_text


@dataclass(slots=True)
class WorkspaceIdentity:
    cwd: Path
    workspace_root: Path
    git_root: Path | None
    origin_url: str | None
    repo_id: str
    workspace_instance_id: str


@dataclass(slots=True)
class MemoryConfig:
    memory_home: Path
    workspace_root: Path
    cwd: Path
    repo_id: str
    workspace_instance_id: str
    token_budget: int = DEFAULT_TOKEN_BUDGET

    @property
    def control_dir(self) -> Path:
        return self.memory_home / "control"

    @property
    def state_db_path(self) -> Path:
        return self.control_dir / "state.sqlite"

    @property
    def index_db_path(self) -> Path:
        return self.control_dir / "index.sqlite"

    @property
    def global_dir(self) -> Path:
        return self.memory_home / "global"

    @property
    def global_memory_path(self) -> Path:
        return self.global_dir / "MEMORY.md"

    @property
    def global_audit_path(self) -> Path:
        return self.global_dir / "audit" / "ops.jsonl"

    @property
    def workspace_memory_dir(self) -> Path:
        return self.memory_home / "workspace"

    @property
    def recent_dir(self) -> Path:
        return self.workspace_memory_dir / "recent"

    @property
    def archive_dir(self) -> Path:
        return self.workspace_memory_dir / "archive"

    @property
    def runtime_dir(self) -> Path:
        return self.workspace_memory_dir / "runtime"

    @property
    def workspace_audit_path(self) -> Path:
        return self.workspace_memory_dir / "audit" / "ops.jsonl"

    @property
    def jobs_dir(self) -> Path:
        return self.control_dir / "jobs"

    def session_snapshot_path(self, session_id: str) -> Path:
        return self.runtime_dir / f"session_{session_id}.json"

    def lock_path(self, kind: str) -> Path:
        return self.control_dir / f"{kind}.lock"


def resolve_config(cwd: str | Path | None = None, memory_home: str | Path | None = None) -> MemoryConfig:
    cwd_path = Path(cwd or os.getcwd()).resolve()
    identity = compute_workspace_identity(cwd_path)
    home_path = _resolve_memory_home(identity, memory_home)
    return MemoryConfig(
        memory_home=home_path,
        workspace_root=identity.workspace_root,
        cwd=identity.cwd,
        repo_id=identity.repo_id,
        workspace_instance_id=identity.workspace_instance_id,
    )


def compute_workspace_identity(cwd: Path) -> WorkspaceIdentity:
    git_root = _git_root(cwd)
    origin_url = _git_origin(cwd if git_root is None else git_root)
    repo_source = _normalize_origin(origin_url) if origin_url else f"repo:{(git_root or cwd).resolve()}"
    repo_id = f"repo_{sha256_text(repo_source)[:12]}"
    workspace_root = (git_root or cwd).resolve()
    workspace_instance_id = f"wsi_{sha256_text(str(workspace_root))[:12]}"
    return WorkspaceIdentity(
        cwd=cwd.resolve(),
        workspace_root=workspace_root,
        git_root=git_root.resolve() if git_root else None,
        origin_url=origin_url,
        repo_id=repo_id,
        workspace_instance_id=workspace_instance_id,
    )


def _git_root(cwd: Path) -> Path | None:
    result = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if result is None:
        return None
    return Path(result)


def _git_origin(cwd: Path) -> str | None:
    return _run_git(["config", "--get", "remote.origin.url"], cwd)


def _run_git(args: list[str], cwd: Path) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _normalize_origin(origin: str) -> str:
    candidate = origin.strip()
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    if candidate.startswith("git@") and ":" in candidate:
        user_host, path = candidate.split(":", 1)
        user, host = user_host.split("@", 1)
        return f"ssh://{user.lower()}@{host.lower()}/{path.strip('/')}"
    return candidate.lower()


def _resolve_memory_home(identity: WorkspaceIdentity, memory_home: str | Path | None) -> Path:
    explicit = memory_home or os.environ.get("CODEX_MEMORY_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    preferred = (Path.home() / ".codex" / "memories" / identity.workspace_instance_id).expanduser()
    if _is_writable_or_creatable(preferred):
        return preferred.resolve()
    return (identity.workspace_root / ".memory-system").resolve()


def _is_writable_or_creatable(path: Path) -> bool:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return os.access(existing, os.W_OK)
