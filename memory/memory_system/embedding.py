from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .utils import sha256_text

TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-./]{1,}")
DEFAULT_DIMENSIONS = 128
DEFAULT_REMOTE_DIMENSIONS = 1024
DEFAULT_QWEN_MODEL = "Qwen/Qwen3-Embedding-4B"
DEFAULT_QWEN_MAX_LENGTH = 8192
DOCUMENT_INSTRUCTION = (
    "为后续检索表示这条开发工作区记忆记录。"
    " Represent this developer-workspace memory record for future retrieval."
)
QUERY_INSTRUCTION = (
    "为检索开发工作区归档记忆表示这个查询。"
    " Represent this query for retrieving archived developer-workspace memory."
)
DEFAULT_ENV_FILE_NAME = ".env"


@dataclass(slots=True)
class EmbeddingSettings:
    provider: str = "auto"
    model_name: str = DEFAULT_QWEN_MODEL
    base_url: str | None = None
    api_key: str | None = None
    endpoint_mode: str = "tei"
    max_length: int = DEFAULT_QWEN_MAX_LENGTH
    dimensions: int | None = DEFAULT_REMOTE_DIMENSIONS


def load_embedding_settings(*, env_file: str | os.PathLike[str] | None = None) -> EmbeddingSettings:
    dotenv = _load_dotenv_file(_resolve_env_file(env_file))
    provider = _config_value("CODEX_MEMORY_EMBEDDING_PROVIDER", dotenv, "auto").strip().lower() or "auto"
    endpoint_mode = _config_value("CODEX_MEMORY_EMBEDDING_ENDPOINT_MODE", dotenv, "tei").strip().lower() or "tei"
    max_length_raw = _config_value("CODEX_MEMORY_EMBEDDING_MAX_LENGTH", dotenv, str(DEFAULT_QWEN_MAX_LENGTH))
    try:
        max_length = max(128, int(max_length_raw))
    except ValueError:
        max_length = DEFAULT_QWEN_MAX_LENGTH
    dimensions_raw = _config_value("CODEX_MEMORY_EMBEDDING_DIMENSIONS", dotenv, str(DEFAULT_REMOTE_DIMENSIONS))
    dimensions = _parse_embedding_dimensions(dimensions_raw)
    return EmbeddingSettings(
        provider=provider,
        model_name=_config_value("CODEX_MEMORY_EMBEDDING_MODEL", dotenv, DEFAULT_QWEN_MODEL).strip() or DEFAULT_QWEN_MODEL,
        base_url=_first_non_empty(
            _config_value("CODEX_MEMORY_EMBEDDING_BASE_URL", dotenv, None),
            _config_value("CODEX_MEMORY_EMBEDDING_ENDPOINT", dotenv, None),  # backward compatibility
        ),
        api_key=_first_non_empty(
            _config_value("CODEX_MEMORY_EMBEDDING_API_KEY", dotenv, None),
        ),
        endpoint_mode=endpoint_mode,
        max_length=max_length,
        dimensions=dimensions,
    )


def embed_document_text(text: str, *, settings: EmbeddingSettings | None = None) -> list[float] | dict[str, float]:
    return _get_embedder(settings).embed_documents([text])[0]


def embed_query_text(text: str, *, settings: EmbeddingSettings | None = None) -> list[float] | dict[str, float]:
    return _get_embedder(settings).embed_queries([text])[0]


def lexical_embedding(text: str, *, dimensions: int = DEFAULT_DIMENSIONS) -> dict[str, float]:
    counts: dict[str, float] = {}
    for token in tokenize(text):
        bucket = str(int(sha256_text(token), 16) % dimensions)
        counts[bucket] = counts.get(bucket, 0.0) + 1.0
    norm = math.sqrt(sum(value * value for value in counts.values()))
    if norm == 0:
        return {}
    return {bucket: value / norm for bucket, value in counts.items()}


def cosine_similarity(left: dict[str, float] | list[float], right: dict[str, float] | list[float]) -> float:
    if not left or not right:
        return 0.0
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return 0.0
        return sum(float(a) * float(b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        if len(left) > len(right):
            left, right = right, left
        score = 0.0
        for key, value in left.items():
            score += value * right.get(key, 0.0)
        return score
    return 0.0


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


class BaseEmbedder:
    provider_name = "base"

    def embed_documents(self, texts: list[str]) -> list[list[float] | dict[str, float]]:
        raise NotImplementedError

    def embed_queries(self, texts: list[str]) -> list[list[float] | dict[str, float]]:
        raise NotImplementedError


class LexicalFallbackEmbedder(BaseEmbedder):
    provider_name = "lexical"

    def __init__(self, settings: EmbeddingSettings | None = None):
        self.settings = settings or EmbeddingSettings(provider="lexical", dimensions=DEFAULT_DIMENSIONS)

    def embed_documents(self, texts: list[str]) -> list[dict[str, float]]:
        return [lexical_embedding(text, dimensions=self._dimensions()) for text in texts]

    def embed_queries(self, texts: list[str]) -> list[dict[str, float]]:
        return [lexical_embedding(f"{QUERY_INSTRUCTION}\n{text}", dimensions=self._dimensions()) for text in texts]

    def _dimensions(self) -> int:
        if self.settings.dimensions is None:
            return DEFAULT_DIMENSIONS
        return max(16, self.settings.dimensions)


class QwenTEIEmbedder(BaseEmbedder):
    provider_name = "qwen_tei"

    def __init__(self, settings: EmbeddingSettings):
        self.settings = settings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._request(texts)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        instructed = [_qwen_query_text(text) for text in texts]
        return self._request(instructed)

    def _request(self, texts: list[str]) -> list[list[float]]:
        import requests

        if not self.settings.base_url:
            raise RuntimeError("Qwen TEI endpoint is not configured")
        if self.settings.endpoint_mode == "openai" and not self.settings.api_key:
            raise RuntimeError("OpenAI-compatible embedding endpoint requires CODEX_MEMORY_EMBEDDING_API_KEY")
        url = _embedding_request_url(self.settings)
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        if self.settings.endpoint_mode == "openai":
            payload: dict[str, Any] = {"model": self.settings.model_name, "input": texts}
            if self.settings.dimensions is not None:
                payload["dimensions"] = self.settings.dimensions
        else:
            payload = {"inputs": texts}
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return [[float(value) for value in row] for row in data]
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [[float(value) for value in row["embedding"]] for row in data["data"]]
        if isinstance(data, dict) and isinstance(data.get("embeddings"), list):
            return [[float(value) for value in row] for row in data["embeddings"]]
        raise RuntimeError(f"unsupported embedding response shape: {json.dumps(data)[:200]}")


class QwenHFEmbedder(BaseEmbedder):
    provider_name = "qwen_hf"

    def __init__(self, settings: EmbeddingSettings):
        self.settings = settings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed([_qwen_query_text(text) for text in texts])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        torch, F, tokenizer, model = _load_local_qwen_model(self.settings.model_name)
        batch = tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=self.settings.max_length,
            return_tensors="pt",
        )
        batch = {key: value.to(model.device) for key, value in batch.items()}
        with torch.no_grad():
            outputs = model(**batch)
        embeddings = _last_token_pool(outputs.last_hidden_state, batch["attention_mask"], torch)
        embeddings = F.normalize(embeddings, p=2, dim=1)
        return embeddings.cpu().tolist()


def _get_embedder(settings: EmbeddingSettings | None) -> BaseEmbedder:
    resolved = settings or load_embedding_settings()
    provider = resolved.provider
    if provider == "lexical":
        return LexicalFallbackEmbedder(resolved)
    if provider == "qwen_tei":
        return QwenTEIEmbedder(resolved)
    if provider == "qwen_hf":
        return QwenHFEmbedder(resolved)
    if provider == "auto":
        if _remote_embedding_enabled(resolved):
            return QwenTEIEmbedder(resolved)
        if _local_qwen_available():
            return QwenHFEmbedder(resolved)
        return LexicalFallbackEmbedder(resolved)
    raise RuntimeError(f"unsupported embedding provider: {provider}")


def _qwen_query_text(query: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery:{query}"


def _local_qwen_available() -> bool:
    try:
        import torch  # noqa: F401
        from transformers import AutoModel  # noqa: F401
        from transformers import AutoTokenizer  # noqa: F401
    except Exception:
        return False
    return True


@lru_cache(maxsize=2)
def _load_local_qwen_model(model_name: str):
    import torch
    import torch.nn.functional as F
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_name, padding_side="left")
    model = AutoModel.from_pretrained(model_name)
    model.eval()
    return torch, F, tokenizer, model


def _last_token_pool(last_hidden_states, attention_mask, torch_module):
    left_padding = attention_mask[:, -1].sum() == attention_mask.shape[0]
    if left_padding:
        return last_hidden_states[:, -1]
    sequence_lengths = attention_mask.sum(dim=1) - 1
    batch_size = last_hidden_states.shape[0]
    return last_hidden_states[torch_module.arange(batch_size, device=last_hidden_states.device), sequence_lengths]


def _memory_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve_env_file(env_file: str | os.PathLike[str] | None) -> Path:
    if env_file is None:
        configured = os.environ.get("CODEX_MEMORY_ENV_FILE", "").strip()
        if configured:
            env_file = configured
        else:
            return _memory_root() / DEFAULT_ENV_FILE_NAME
    path = Path(env_file).expanduser()
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


def _load_dotenv_file(path: Path) -> dict[str, str]:
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
        values[key] = _parse_dotenv_value(raw_value.strip())
    return values


def _parse_dotenv_value(value: str) -> str:
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


def _config_value(name: str, dotenv: dict[str, str], default: str | None) -> str:
    if name in os.environ:
        return os.environ[name]
    if name in dotenv:
        return dotenv[name]
    return default or ""


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _embedding_request_url(settings: EmbeddingSettings) -> str:
    if not settings.base_url:
        raise RuntimeError("Qwen base URL is not configured")
    url = settings.base_url.rstrip("/")
    if settings.endpoint_mode == "tei":
        return url if url.endswith("/embed") else f"{url}/embed"
    if settings.endpoint_mode == "openai":
        return url if url.endswith("/embeddings") else f"{url}/embeddings"
    return url


def _remote_embedding_enabled(settings: EmbeddingSettings) -> bool:
    if not settings.base_url:
        return False
    if settings.endpoint_mode == "openai" and not settings.api_key:
        return False
    return True


def _parse_embedding_dimensions(raw: str) -> int | None:
    candidate = (raw or "").strip()
    if not candidate:
        return None
    try:
        value = int(candidate)
    except ValueError:
        return DEFAULT_REMOTE_DIMENSIONS
    return max(16, value)
