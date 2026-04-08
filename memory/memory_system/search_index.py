from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import MemoryConfig
from .constants import GLOBAL_SCOPE, LOCAL_ARCHIVE_SCOPE, LOCAL_RECENT_SCOPE
from .embedding import cosine_similarity, embed_document_text, embed_query_text
from .markdown_store import all_records, load_document
from .workspace_store import document_repo_id, document_workspace_instance_id


class SearchIndex:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
              record_id TEXT PRIMARY KEY,
              scope TEXT NOT NULL,
              repo_id TEXT,
              workspace_instance_id TEXT,
              source_path TEXT NOT NULL,
              status TEXT NOT NULL,
              subject TEXT NOT NULL,
              summary TEXT NOT NULL,
              tags_json TEXT NOT NULL,
              created_at TEXT,
              updated_at TEXT,
              content TEXT NOT NULL,
              vector_json TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS records_fts
              USING fts5(record_id UNINDEXED, content);
            """
        )
        self._ensure_column("records", "vector_json", "TEXT NOT NULL DEFAULT '{}'")
        self.conn.commit()

    def rebuild(self, config: MemoryConfig) -> int:
        self.conn.execute("DELETE FROM records")
        self.conn.execute("DELETE FROM records_fts")
        count = 0
        count += self._index_document(config.global_memory_path, GLOBAL_SCOPE)
        for path in sorted(config.recent_dir.glob("*.md")):
            count += self._index_document(path, LOCAL_RECENT_SCOPE)
        for path in sorted(config.archive_dir.glob("*/*/*.md")):
            count += self._index_document(path, LOCAL_ARCHIVE_SCOPE)
        self.conn.commit()
        return count

    def search_old(
        self,
        *,
        workspace_instance_id: str,
        query: str,
        top_k: int = 8,
        repo_id: str | None = None,
        search_scope: str = "current_workspace",
    ) -> list[dict[str, Any]]:
        params: list[Any] = [LOCAL_ARCHIVE_SCOPE]
        where = ["scope = ?"]
        if search_scope == "same_repo" and repo_id:
            where.append("repo_id = ?")
            params.append(repo_id)
        else:
            where.append("workspace_instance_id = ?")
            params.append(workspace_instance_id)
        if not query.strip():
            sql = f"""
                SELECT record_id, repo_id, workspace_instance_id, source_path, status,
                       subject, summary, tags_json, created_at, updated_at,
                       summary AS snippet, 0.0 AS score
                FROM records
                WHERE {' AND '.join(where)}
                ORDER BY updated_at DESC, record_id ASC
                LIMIT ?
            """
            params.append(top_k)
            rows = self.conn.execute(sql, params).fetchall()
            return [_row_to_result(row, float(row["score"])) for row in rows]

        vector_rows = self.conn.execute(
            f"""
            SELECT record_id, repo_id, workspace_instance_id, source_path, status,
                   subject, summary, tags_json, created_at, updated_at, content, vector_json
            FROM records
            WHERE {' AND '.join(where)}
            """,
            params,
        ).fetchall()
        query_vector = embed_query_text(query)
        vector_ranked = sorted(
            (
                (
                    row["record_id"],
                    cosine_similarity(query_vector, json.loads(row["vector_json"])),
                    row,
                )
                for row in vector_rows
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:40]
        fts_sql = f"""
            SELECT records.record_id, records.repo_id, records.workspace_instance_id,
                   records.source_path, records.status, records.subject, records.summary,
                   records.tags_json, records.created_at, records.updated_at,
                   records.content, records.vector_json,
                   snippet(records_fts, 1, '[', ']', '...', 16) AS snippet,
                   bm25(records_fts) AS bm25_score
            FROM records_fts
            JOIN records ON records.record_id = records_fts.record_id
            WHERE {' AND '.join(where)} AND records_fts MATCH ?
            ORDER BY bm25_score
            LIMIT 20
        """
        fts_rows = self.conn.execute(fts_sql, [*params, query]).fetchall()
        fts_ranked = [(row["record_id"], row["bm25_score"], row) for row in fts_rows]

        fused: dict[str, dict[str, Any]] = {}
        for rank, (_, score, row) in enumerate(vector_ranked, start=1):
            entry = fused.setdefault(row["record_id"], {"row": row, "score": 0.0})
            if score > 0:
                entry["score"] += 1.0 / (20 + rank)
        for rank, (_, _, row) in enumerate(fts_ranked, start=1):
            entry = fused.setdefault(row["record_id"], {"row": row, "score": 0.0})
            entry["score"] += 1.0 / (20 + rank)
            entry["snippet"] = row["snippet"]
        ranked = sorted(fused.values(), key=lambda item: item["score"], reverse=True)[:top_k]
        return [
            _row_to_result(
                item["row"],
                item["score"],
                snippet=item.get("snippet") or item["row"]["summary"],
            )
            for item in ranked
        ]

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT record_id, scope, repo_id, workspace_instance_id, source_path, status,
                   subject, summary, tags_json, created_at, updated_at, content
            FROM records
            WHERE record_id = ?
            """,
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "record_id": row["record_id"],
            "scope": row["scope"],
            "repo_id": row["repo_id"],
            "workspace_instance_id": row["workspace_instance_id"],
            "source_path": row["source_path"],
            "status": row["status"],
            "subject": row["subject"],
            "summary": row["summary"],
            "tags": json.loads(row["tags_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "content": row["content"],
        }

    def _index_document(
        self,
        path: Path,
        scope: str,
    ) -> int:
        if not path.exists():
            return 0
        document = load_document(path, scope)
        repo_id = None if scope == GLOBAL_SCOPE else document_repo_id(document)
        workspace_instance_id = None if scope == GLOBAL_SCOPE else document_workspace_instance_id(document)
        count = 0
        for record in all_records(document):
            content = "\n".join(
                filter(
                    None,
                    [
                        record.subject,
                        record.summary,
                        record.rationale,
                        record.next_use,
                        " ".join(record.tags),
                    ],
                )
            )
            self.conn.execute(
                """
                INSERT INTO records(
                  record_id, scope, repo_id, workspace_instance_id, source_path, status,
                  subject, summary, tags_json, created_at, updated_at, content, vector_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    scope,
                    repo_id,
                    workspace_instance_id,
                    str(path),
                    record.status,
                    record.subject,
                    record.summary,
                    json.dumps(record.tags, ensure_ascii=True),
                    record.created_at,
                    record.updated_at,
                    content,
                    json.dumps(embed_document_text(content), ensure_ascii=True, sort_keys=True),
                ),
            )
            self.conn.execute(
                "INSERT INTO records_fts(record_id, content) VALUES (?, ?)",
                (record.id, content),
            )
            count += 1
        return count

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _row_to_result(row: sqlite3.Row, fused_score: float, snippet: str | None = None) -> dict[str, Any]:
    return {
        "record_id": row["record_id"],
        "repo_id": row["repo_id"],
        "workspace_instance_id": row["workspace_instance_id"],
        "source_path": row["source_path"],
        "status": row["status"],
        "subject": row["subject"],
        "summary": row["summary"],
        "snippet": snippet or row["summary"],
        "score": fused_score,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "tags": json.loads(row["tags_json"]),
    }


def search_old_records(
    config: MemoryConfig,
    *,
    query: str,
    top_k: int = 8,
    search_scope: str = "current_workspace",
    workspace_instance_id: str | None = None,
    repo_id: str | None = None,
) -> list[dict[str, Any]]:
    target_workspace_instance_id = workspace_instance_id or config.workspace_instance_id
    target_repo_id = repo_id or config.repo_id
    index_paths = _candidate_index_paths(
        config,
        include_peers=search_scope == "same_repo" or target_workspace_instance_id != config.workspace_instance_id,
    )
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    per_index_limit = max(top_k, 8)
    for index_path in index_paths:
        index = SearchIndex(index_path)
        try:
            rows = index.search_old(
                workspace_instance_id=target_workspace_instance_id,
                repo_id=target_repo_id,
                query=query,
                top_k=per_index_limit,
                search_scope=search_scope,
            )
        finally:
            index.close()
        for row in rows:
            key = (
                str(row.get("record_id", "")),
                str(row.get("workspace_instance_id") or ""),
                str(row.get("source_path") or ""),
            )
            previous = merged.get(key)
            if previous is None or _result_sort_key(row) > _result_sort_key(previous):
                merged[key] = row
    return sorted(merged.values(), key=_result_sort_key, reverse=True)[:top_k]


def _candidate_index_paths(config: MemoryConfig, *, include_peers: bool) -> list[Path]:
    current = config.index_db_path.resolve()
    candidates = [current]
    if not include_peers:
        return candidates
    parent = config.memory_home.parent
    if not parent.exists():
        return candidates
    seen = {current}
    for sibling in sorted(parent.iterdir()):
        candidate = sibling / "control" / "index.sqlite"
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(resolved)
    return candidates


def _result_sort_key(row: dict[str, Any]) -> tuple[float, str, str, str]:
    return (
        float(row.get("score", 0.0)),
        str(row.get("updated_at") or ""),
        str(row.get("created_at") or ""),
        str(row.get("record_id") or ""),
    )
