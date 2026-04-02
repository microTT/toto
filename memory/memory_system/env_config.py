from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE_NAME = ".env"


def memory_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_env_file(env_file: str | os.PathLike[str] | None) -> Path:
    if env_file is None:
        configured = os.environ.get("CODEX_MEMORY_ENV_FILE", "").strip()
        if configured:
            env_file = configured
        else:
            return memory_root() / DEFAULT_ENV_FILE_NAME
    path = Path(env_file).expanduser()
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


def load_dotenv_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = parse_dotenv_value(raw_value.strip())
    return values


def parse_dotenv_value(value: str) -> str:
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        if value[0] == '"':
            return bytes(inner, "utf-8").decode("unicode_escape")
        return inner
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def config_value(name: str, dotenv: dict[str, str], default: str | None) -> str:
    if name in os.environ:
        return os.environ[name]
    if name in dotenv:
        return dotenv[name]
    return default or ""


def first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return None
