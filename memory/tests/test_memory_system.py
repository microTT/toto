from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_system.admin import main as admin_main
from memory_system.archive import archive_stale_recent_documents
from memory_system.bootstrap import ensure_layout
from memory_system.config import MemoryConfig, resolve_config
from memory_system.constants import GLOBAL_SCOPE, LOCAL_RECENT_SCOPE, STATUS_ACTIVE, STATUS_OPEN, STATUS_SUPERSEDED
from memory_system.embedding import embed_document_text, embed_query_text, load_embedding_settings
from memory_system.errors import PatchApplyError, SummarizerExecutionError
from memory_system.hooks import run_hook
from memory_system.markdown_store import all_records, load_document
from memory_system.patch_applier import apply_patch_plan, current_base_revisions
from memory_system.record_store import find_record
from memory_system.search_index import SearchIndex, search_old_records
from memory_system.snapshot import build_snapshot
from memory_system.state_db import StateDB
from memory_system.worker import run_worker_once


class MemorySystemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.memory_home = Path(self.temp_dir.name) / "memory-home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "CODEX_MEMORY_HOME": str(self.memory_home),
                "CODEX_MEMORY_EMBEDDING_PROVIDER": "lexical",
                "CODEX_MEMORY_ENV_FILE": str(self.memory_home / "missing.env"),
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.config = resolve_config(self.workspace)
        ensure_layout(self.config)

    def test_manual_upsert_and_snapshot_context(self) -> None:
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "global",
                "--type",
                "preference",
                "--subject",
                "package manager",
                "--summary",
                "Prefer pnpm unless repo requires npm",
                "--tags",
                "javascript,tooling",
                "--scope-reason",
                "cross-workspace and durable",
            ]
        )
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "local",
                "--type",
                "task_context",
                "--subject",
                "auth flaky tests",
                "--summary",
                "Snapshots fail on CI",
                "--next-use",
                "re-check snapshots before auth middleware",
                "--scope-reason",
                "repo-specific and near-term",
            ]
        )
        snapshot = build_snapshot(self.config)
        self.assertIn("package manager", snapshot.rendered_text)
        self.assertIn("auth flaky tests", snapshot.rendered_text)

    def test_manual_upsert_with_explicit_id_creates_then_updates(self) -> None:
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "global",
                "--id",
                "g_manual_explicit_id",
                "--type",
                "preference",
                "--subject",
                "explicit id upsert",
                "--summary",
                "initial summary",
                "--scope-reason",
                "cross-workspace and durable",
            ]
        )
        first = find_record(self.config, "g_manual_explicit_id")
        self.assertIsNotNone(first)
        self.assertEqual(first[3].summary, "initial summary")

        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "global",
                "--id",
                "g_manual_explicit_id",
                "--type",
                "preference",
                "--subject",
                "explicit id upsert",
                "--summary",
                "updated summary",
                "--scope-reason",
                "cross-workspace and durable",
            ]
        )
        second = find_record(self.config, "g_manual_explicit_id")
        self.assertIsNotNone(second)
        self.assertEqual(second[3].summary, "updated summary")

    def test_archive_and_search_index(self) -> None:
        old_path = self.config.recent_dir / "2020-01-01.md"
        patch_plan = {
            "decision": "write",
            "reason": "seed old records",
            "base_revisions": current_base_revisions(self.config),
            "global_ops": [],
            "local_ops": [
                {
                    "action": "create",
                    "record": {
                        "id": "l_old_closed",
                        "type": "failed_attempt",
                        "status": "closed",
                        "confidence": "high",
                        "subject": "old failed attempt",
                        "summary": "rerunning snapshots without clearing cache did not help",
                        "tags": ["auth", "ci"],
                        "source_refs": [],
                        "scope_reason": "repo-specific and near-term",
                    },
                }
            ],
            "needs_manual_review": False,
        }
        apply_patch_plan(self.config, patch_plan, now=_dt("2020-01-01T10:00:00Z"))
        current_revisions = current_base_revisions(self.config, now=_dt("2020-01-03T10:00:00Z"))
        pin_plan = {
            "decision": "write",
            "reason": "seed pinned old record",
            "base_revisions": current_revisions,
            "global_ops": [],
            "local_ops": [
                {
                    "action": "create",
                    "record": {
                        "id": "l_old_open",
                        "type": "task_context",
                        "status": "open",
                        "confidence": "high",
                        "subject": "long running issue",
                        "summary": "keep tracking the long running issue",
                        "tags": ["tracking"],
                        "source_refs": [],
                        "scope_reason": "repo-specific and near-term",
                        "pin_until": "2099-01-01T00:00:00Z",
                    },
                }
            ],
            "needs_manual_review": False,
        }
        apply_patch_plan(self.config, pin_plan, now=_dt("2020-01-01T11:00:00Z"))
        archived = archive_stale_recent_documents(self.config, now=_dt("2020-01-03T10:00:00Z"))
        self.assertTrue(archived)
        self.assertTrue(old_path.exists())
        index = SearchIndex(self.config.index_db_path)
        try:
            indexed = index.rebuild(self.config)
            self.assertGreaterEqual(indexed, 1)
            results = index.search_old(
                workspace_instance_id=self.config.workspace_instance_id,
                query="snapshots cache",
                top_k=4,
            )
        finally:
            index.close()
        self.assertEqual(results[0]["record_id"], "l_old_closed")

    def test_same_repo_search_federates_across_peer_memory_homes(self) -> None:
        peer_workspace = Path(self.temp_dir.name) / "workspace-peer"
        peer_workspace.mkdir(parents=True, exist_ok=True)
        peer_config = MemoryConfig(
            memory_home=Path(self.temp_dir.name) / "memory-peer",
            workspace_root=peer_workspace,
            cwd=peer_workspace,
            repo_id=self.config.repo_id,
            workspace_instance_id="wsi_peer000001",
        )
        ensure_layout(peer_config)
        peer_plan = {
            "decision": "write",
            "reason": "seed peer archive",
            "base_revisions": current_base_revisions(peer_config, now=_dt("2020-01-01T10:00:00Z")),
            "global_ops": [],
            "local_ops": [
                {
                    "action": "create",
                    "record": {
                        "id": "l_peer_closed",
                        "type": "failed_attempt",
                        "status": "closed",
                        "confidence": "high",
                        "subject": "peer auth snapshot attempt",
                        "summary": "Peer clone already observed the auth snapshot failure.",
                        "tags": ["auth", "peer"],
                        "source_refs": [],
                        "scope_reason": "repo-specific and near-term",
                    },
                }
            ],
            "needs_manual_review": False,
        }
        apply_patch_plan(peer_config, peer_plan, now=_dt("2020-01-01T10:00:00Z"))
        archive_stale_recent_documents(peer_config, now=_dt("2020-01-03T10:00:00Z"))
        index = SearchIndex(peer_config.index_db_path)
        try:
            index.rebuild(peer_config)
        finally:
            index.close()

        current_scope_results = search_old_records(
            self.config,
            query="peer auth snapshot",
            top_k=3,
            search_scope="current_workspace",
        )
        same_repo_results = search_old_records(
            self.config,
            query="peer auth snapshot",
            top_k=3,
            search_scope="same_repo",
        )
        self.assertEqual(current_scope_results, [])
        self.assertEqual(same_repo_results[0]["record_id"], "l_peer_closed")
        self.assertEqual(same_repo_results[0]["workspace_instance_id"], "wsi_peer000001")

    def test_hook_to_worker_flow(self) -> None:
        run_hook(
            "session-start",
            {
                "session_id": "session-1",
                "turn_id": "turn-0",
                "cwd": str(self.workspace),
            },
        )
        context = run_hook(
            "user-prompt-submit",
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "cwd": str(self.workspace),
                "user_message_delta": "remember next step: revisit the failing auth snapshot",
            },
        )
        context_payload = json.loads(context)
        self.assertEqual(context_payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("[MEMORY LOADED]", context_payload["hookSpecificOutput"]["additionalContext"])
        stop_result = run_hook(
            "stop",
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "cwd": str(self.workspace),
                "user_message_delta": "remember next step: revisit the failing auth snapshot",
                "assistant_message_delta": "Will keep that next step in mind.",
            },
        )
        self.assertEqual(stop_result, "")
        worker_result = run_worker_once(str(self.workspace), backend="heuristic")
        self.assertTrue(worker_result["applied"])
        snapshot = build_snapshot(self.config)
        self.assertIn("revisit the failing auth snapshot", snapshot.rendered_text)

    def test_migrate_zh_translates_existing_memory_records(self) -> None:
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "global",
                "--id",
                "g_name_pref",
                "--type",
                "preference",
                "--subject",
                "Assistant name preference",
                "--summary",
                "User wants the assistant to be called 思绎 in future conversations.",
                "--rationale",
                "The user explicitly said: 记住,你以后叫 思绎.",
                "--next-use",
                "Refer to the assistant as 思绎 in future conversations.",
                "--tags",
                "global,preference,naming",
                "--scope-reason",
                "This is a durable user preference that applies across repositories and future sessions, so it belongs in global memory.",
            ]
        )
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "local",
                "--id",
                "l_launchd_todo",
                "--type",
                "todo",
                "--subject",
                "Launchd daemon smoke test",
                "--summary",
                "Near-term repo-specific next step: run a launchd daemon smoke test.",
                "--next-use",
                "Surface when resuming work in this repository as an immediate follow-up task.",
                "--tags",
                "repo-specific,near-term,todo,launchd",
                "--scope-reason",
                "repo-specific and near-term",
            ]
        )

        payload = json.loads(
            self._run_admin(["--cwd", str(self.workspace), "migrate-zh"])
        )
        self.assertEqual(payload["changed_records"], 2)

        global_match = find_record(self.config, "g_name_pref")
        self.assertIsNotNone(global_match)
        global_record = global_match[3]
        self.assertEqual(global_record.subject, "助手名称偏好")
        self.assertEqual(global_record.summary, "用户希望在后续对话中把助手称为 思绎。")
        self.assertEqual(global_record.next_use, "后续对话中将助手称为 思绎。")
        self.assertIn("用户明确说过", global_record.rationale)

        local_match = find_record(self.config, "l_launchd_todo")
        self.assertIsNotNone(local_match)
        local_record = local_match[3]
        self.assertEqual(local_record.subject, "Launchd 常驻进程冒烟测试")
        self.assertEqual(local_record.summary, "当前仓库近期下一步：执行一次 Launchd 常驻进程冒烟测试。")
        self.assertEqual(local_record.next_use, "恢复该仓库工作时，优先展示并继续这项任务。")

    def test_patch_rejects_stale_revision(self) -> None:
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "global",
                "--type",
                "preference",
                "--subject",
                "package manager",
                "--summary",
                "Prefer pnpm",
                "--scope-reason",
                "cross-workspace and durable",
            ]
        )
        stale_plan = {
            "decision": "write",
            "reason": "stale revision test",
            "base_revisions": {"global_revision": 0, "local_recent_revision": 0},
            "global_ops": [],
            "local_ops": [],
            "needs_manual_review": False,
        }
        with self.assertRaises(PatchApplyError):
            apply_patch_plan(self.config, stale_plan)

    def test_patch_rejects_secret_like_content(self) -> None:
        plan = {
            "decision": "write",
            "reason": "secret filter test",
            "base_revisions": current_base_revisions(self.config),
            "global_ops": [
                {
                    "action": "create",
                    "record": {
                        "type": "preference",
                        "status": "active",
                        "confidence": "high",
                        "subject": "bad secret",
                        "summary": "api_key=sk-verysecretvalue12345",
                        "tags": [],
                        "source_refs": [],
                        "scope_reason": "should be rejected",
                    },
                }
            ],
            "local_ops": [],
            "needs_manual_review": False,
        }
        with self.assertRaises(PatchApplyError):
            apply_patch_plan(self.config, plan)

    def test_promote_local_record_to_global(self) -> None:
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "local",
                "--type",
                "task_context",
                "--subject",
                "package manager preference",
                "--summary",
                "Prefer pnpm in this repo",
                "--tags",
                "javascript,tooling",
                "--scope-reason",
                "repo-specific and near-term",
            ]
        )
        local_id = self._record_id_by_subject(LOCAL_RECENT_SCOPE, "package manager preference")
        result = apply_patch_plan(
            self.config,
            {
                "decision": "write",
                "reason": "promote repeated local preference",
                "base_revisions": current_base_revisions(self.config),
                "global_ops": [],
                "local_ops": [
                    {
                        "action": "promote",
                        "target_id": local_id,
                        "replacement_record": {
                            "type": "preference",
                            "confidence": "high",
                            "subject": "package manager preference",
                            "summary": "Prefer pnpm unless the repo explicitly requires another package manager.",
                            "tags": ["javascript", "tooling", "pnpm"],
                            "source_refs": ["promotion"],
                            "scope_reason": "cross-workspace and durable",
                        },
                    }
                ],
                "needs_manual_review": False,
            },
        )
        self.assertGreaterEqual(result["global_revision"], 1)
        global_document = load_document(self.config.global_memory_path, GLOBAL_SCOPE)
        promoted = next(record for record in all_records(global_document) if record.subject == "package manager preference")
        self.assertEqual(promoted.status, STATUS_ACTIVE)
        self.assertEqual(promoted.supersedes, [local_id])

        local_document = load_document(next(self.config.recent_dir.glob("*.md")), LOCAL_RECENT_SCOPE)
        original = next(record for record in all_records(local_document) if record.id == local_id)
        self.assertEqual(original.status, STATUS_SUPERSEDED)
        self.assertEqual(original.superseded_by, promoted.id)

    def test_demote_global_record_to_local(self) -> None:
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "global",
                "--type",
                "fact",
                "--subject",
                "auth snapshot issue",
                "--summary",
                "Auth snapshots are flaky in this repo only",
                "--tags",
                "auth,ci",
                "--scope-reason",
                "cross-workspace and durable",
            ]
        )
        global_id = self._record_id_by_subject(GLOBAL_SCOPE, "auth snapshot issue")
        result = apply_patch_plan(
            self.config,
            {
                "decision": "write",
                "reason": "demote repo-specific fact",
                "base_revisions": current_base_revisions(self.config),
                "global_ops": [
                    {
                        "action": "demote",
                        "target_id": global_id,
                        "replacement_record": {
                            "type": "task_context",
                            "confidence": "high",
                            "subject": "auth snapshot issue",
                            "summary": "Auth snapshots are flaky in this repository and need local follow-up.",
                            "tags": ["auth", "ci"],
                            "source_refs": ["demotion"],
                            "scope_reason": "repo-specific and near-term",
                        },
                    }
                ],
                "local_ops": [],
                "needs_manual_review": False,
            },
        )
        self.assertGreaterEqual(result["local_recent_revision"], 1)
        global_document = load_document(self.config.global_memory_path, GLOBAL_SCOPE)
        demoted = next(record for record in all_records(global_document) if record.id == global_id)
        self.assertEqual(demoted.status, STATUS_SUPERSEDED)

        local_document = load_document(next(self.config.recent_dir.glob("*.md")), LOCAL_RECENT_SCOPE)
        local_copy = next(record for record in all_records(local_document) if record.subject == "auth snapshot issue")
        self.assertEqual(local_copy.status, STATUS_OPEN)
        self.assertEqual(local_copy.supersedes, [global_id])
        self.assertEqual(demoted.superseded_by, local_copy.id)

    def test_worker_retries_transient_summarizer_failure(self) -> None:
        run_hook(
            "stop",
            {
                "session_id": "retry-session",
                "turn_id": "turn-1",
                "cwd": str(self.workspace),
                "user_message_delta": "remember next step: retry the auth snapshot flow",
                "assistant_message_delta": "noted",
            },
        )
        transient_error = SummarizerExecutionError("temporary codex backend failure")
        successful_patch = {
            "decision": "write",
            "reason": "retry success",
            "base_revisions": current_base_revisions(self.config),
            "global_ops": [],
            "local_ops": [
                {
                    "action": "create",
                    "record": {
                        "type": "todo",
                        "status": "open",
                        "confidence": "high",
                        "subject": "retry auth snapshot flow",
                        "summary": "Retry the auth snapshot flow.",
                        "tags": ["todo", "auth"],
                        "source_refs": ["transcript_delta"],
                        "scope_reason": "repo-specific and near-term",
                    },
                }
            ],
            "needs_manual_review": False,
        }
        with mock.patch(
            "memory_system.worker.summarize_job",
            side_effect=[transient_error, successful_patch],
        ):
            first = run_worker_once(
                str(self.workspace),
                memory_home=str(self.memory_home),
                backend="codex",
                retry_base_seconds=0,
            )
            self.assertEqual(first["retry_status"]["status"], "retry_wait")
            state = sqlite3.connect(self.config.state_db_path)
            try:
                row = state.execute(
                    "SELECT status, attempt_count, last_error FROM summary_jobs WHERE session_id = ?",
                    ("retry-session",),
                ).fetchone()
            finally:
                state.close()
            self.assertEqual(row[0], "retry_wait")
            self.assertEqual(row[1], 1)
            self.assertIn("temporary codex backend failure", row[2])

            second = run_worker_once(
                str(self.workspace),
                memory_home=str(self.memory_home),
                backend="codex",
                retry_base_seconds=0,
            )
        self.assertTrue(second["applied"])
        snapshot = build_snapshot(self.config)
        self.assertIn("retry auth snapshot flow", snapshot.rendered_text)

    def test_worker_gc_deletes_old_runtime_snapshots_and_finished_jobs(self) -> None:
        old_runtime = self.config.runtime_dir / "session_old.json"
        old_runtime.write_text("{}", encoding="utf-8")
        old_timestamp = _dt("2020-01-01T00:00:00Z").timestamp()
        os.utime(old_runtime, (old_timestamp, old_timestamp))

        state = StateDB(self.config.state_db_path)
        try:
            job = state.upsert_summary_job(
                job_key="gc-job",
                session_id="gc-session",
                repo_id=self.config.repo_id,
                workspace_instance_id=self.config.workspace_instance_id,
                workspace_root=str(self.config.workspace_root),
                transcript_path=None,
                start_event_id=None,
                end_event_id=None,
                prompt_version="v1",
                reason="gc test",
                payload={"kind": "gc"},
            )
            state.update_job_status(job.id, "completed", {"kind": "gc"})
            state.conn.execute(
                "UPDATE summary_jobs SET updated_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", job.id),
            )
            state.conn.commit()
        finally:
            state.close()

        result = run_worker_once(
            str(self.workspace),
            memory_home=str(self.memory_home),
            backend="heuristic",
            completed_job_retention_days=0,
            failed_job_retention_days=0,
        )
        self.assertFalse(old_runtime.exists())
        self.assertEqual(result["deleted_job_count"], 1)
        self.assertIn(str(old_runtime), result["deleted_runtime_snapshots"])

    def test_qwen_tei_embedding_adapter_applies_query_instruction(self) -> None:
        captured_requests: list[tuple[str, dict[str, object], dict[str, str] | None]] = []

        env_file = self.memory_home / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "CODEX_MEMORY_EMBEDDING_PROVIDER=qwen_tei",
                    "CODEX_MEMORY_EMBEDDING_BASE_URL=http://localhost:8080",
                    "CODEX_MEMORY_EMBEDDING_API_KEY=test-token",
                    "CODEX_MEMORY_EMBEDDING_MODEL=Qwen/test-model",
                    "CODEX_MEMORY_EMBEDDING_ENDPOINT_MODE=tei",
                    "CODEX_MEMORY_EMBEDDING_DIMENSIONS=1536",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return self._payload

        def fake_post(url, json=None, headers=None, timeout=None):
            captured_requests.append((url, json, headers))
            size = len(json["inputs"])
            return FakeResponse([[float(index + 1)] * 3 for index in range(size)])

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("requests.post", side_effect=fake_post):
            settings = load_embedding_settings(env_file=env_file)
            document_vector = embed_document_text("alpha document", settings=settings)
            query_vector = embed_query_text("auth snapshot", settings=settings)

        self.assertEqual(document_vector, [1.0, 1.0, 1.0])
        self.assertEqual(query_vector, [1.0, 1.0, 1.0])
        self.assertEqual(captured_requests[0][0], "http://localhost:8080/embed")
        self.assertEqual(captured_requests[0][1]["inputs"], ["alpha document"])
        self.assertEqual(captured_requests[0][2]["Authorization"], "Bearer test-token")
        query_instruction = captured_requests[1][1]["inputs"][0]
        self.assertTrue(query_instruction.startswith("Instruct: 为检索开发工作区归档记忆表示这个查询。"))
        self.assertIn("Represent this query for retrieving archived developer-workspace memory.", query_instruction)
        self.assertIn("Query:auth snapshot", query_instruction)

    def test_embedding_settings_load_from_dotenv_file(self) -> None:
        env_file = self.memory_home / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "CODEX_MEMORY_EMBEDDING_PROVIDER=qwen_tei",
                    "CODEX_MEMORY_EMBEDDING_BASE_URL=http://localhost:9000/v1",
                    "CODEX_MEMORY_EMBEDDING_API_KEY=from-dotenv",
                    "CODEX_MEMORY_EMBEDDING_MODEL=Qwen/custom-model",
                    "CODEX_MEMORY_EMBEDDING_ENDPOINT_MODE=openai",
                    "CODEX_MEMORY_EMBEDDING_MAX_LENGTH=4096",
                    "CODEX_MEMORY_EMBEDDING_DIMENSIONS=768",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            settings = load_embedding_settings(env_file=env_file)

        self.assertEqual(settings.provider, "qwen_tei")
        self.assertEqual(settings.base_url, "http://localhost:9000/v1")
        self.assertEqual(settings.api_key, "from-dotenv")
        self.assertEqual(settings.model_name, "Qwen/custom-model")
        self.assertEqual(settings.endpoint_mode, "openai")
        self.assertEqual(settings.max_length, 4096)
        self.assertEqual(settings.dimensions, 768)

    def test_environment_overrides_dotenv_embedding_settings(self) -> None:
        env_file = self.memory_home / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "CODEX_MEMORY_EMBEDDING_PROVIDER=qwen_tei",
                    "CODEX_MEMORY_EMBEDDING_BASE_URL=http://localhost:9000",
                    "CODEX_MEMORY_EMBEDDING_API_KEY=from-dotenv",
                    "CODEX_MEMORY_EMBEDDING_MODEL=Qwen/from-dotenv",
                    "CODEX_MEMORY_EMBEDDING_DIMENSIONS=768",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(
            os.environ,
            {
                "CODEX_MEMORY_EMBEDDING_PROVIDER": "lexical",
                "CODEX_MEMORY_EMBEDDING_BASE_URL": "http://localhost:7000",
                "CODEX_MEMORY_EMBEDDING_API_KEY": "from-env",
                "CODEX_MEMORY_EMBEDDING_MODEL": "Qwen/from-env",
                "CODEX_MEMORY_EMBEDDING_DIMENSIONS": "512",
            },
            clear=True,
        ):
            settings = load_embedding_settings(env_file=env_file)

        self.assertEqual(settings.provider, "lexical")
        self.assertEqual(settings.base_url, "http://localhost:7000")
        self.assertEqual(settings.api_key, "from-env")
        self.assertEqual(settings.model_name, "Qwen/from-env")
        self.assertEqual(settings.dimensions, 512)

    def test_openai_embedding_request_includes_dimensions(self) -> None:
        captured_requests: list[tuple[str, dict[str, object], dict[str, str] | None]] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured_requests.append((url, json, headers))
            return FakeResponse()

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("requests.post", side_effect=fake_post):
            vector = embed_document_text(
                "alpha",
                settings=load_embedding_settings(
                    env_file=self._write_env(
                        [
                            "CODEX_MEMORY_EMBEDDING_PROVIDER=qwen_tei",
                            "CODEX_MEMORY_EMBEDDING_BASE_URL=https://example.com/v1",
                            "CODEX_MEMORY_EMBEDDING_API_KEY=test-token",
                            "CODEX_MEMORY_EMBEDDING_MODEL=text-embedding-v4",
                            "CODEX_MEMORY_EMBEDDING_ENDPOINT_MODE=openai",
                            "CODEX_MEMORY_EMBEDDING_DIMENSIONS=1024",
                        ]
                    )
                ),
            )

        self.assertEqual(vector, [0.1, 0.2, 0.3])
        self.assertEqual(captured_requests[0][0], "https://example.com/v1/embeddings")
        self.assertEqual(captured_requests[0][1]["dimensions"], 1024)
        self.assertEqual(captured_requests[0][1]["model"], "text-embedding-v4")
        self.assertEqual(captured_requests[0][2]["Authorization"], "Bearer test-token")

    def test_lexical_provider_uses_configured_dimensions(self) -> None:
        settings = load_embedding_settings(
            env_file=self._write_env(
                [
                    "CODEX_MEMORY_EMBEDDING_PROVIDER=lexical",
                    "CODEX_MEMORY_EMBEDDING_DIMENSIONS=32",
                ]
            )
        )
        vector = embed_document_text("alpha beta gamma", settings=settings)
        self.assertTrue(vector)
        for bucket in vector:
            self.assertLess(int(bucket), 32)

    def test_auto_provider_without_api_key_falls_back_to_lexical(self) -> None:
        settings = load_embedding_settings(
            env_file=self._write_env(
                [
                    "CODEX_MEMORY_EMBEDDING_PROVIDER=auto",
                    "CODEX_MEMORY_EMBEDDING_BASE_URL=https://example.com/v1",
                    "CODEX_MEMORY_EMBEDDING_MODEL=text-embedding-v4",
                    "CODEX_MEMORY_EMBEDDING_ENDPOINT_MODE=openai",
                    "CODEX_MEMORY_EMBEDDING_DIMENSIONS=64",
                ]
            )
        )
        with mock.patch("requests.post") as post_mock:
            vector = embed_document_text("alpha beta", settings=settings)
        self.assertTrue(vector)
        self.assertFalse(post_mock.called)

    def test_patch_schema_is_codex_compatible_flat_transport_schema(self) -> None:
        schema = json.loads((ROOT / "schemas" / "memory_patch.schema.json").read_text(encoding="utf-8"))
        self.assertIn("op", schema.get("$defs", {}))
        self.assertNotIn("oneOf", schema["$defs"]["op"])
        self.assertIn("promote", schema["$defs"]["op"]["properties"]["action"]["enum"])
        self.assertIn("demote", schema["$defs"]["op"]["properties"]["action"]["enum"])
        self.assertEqual(
            sorted(schema["$defs"]["op"]["required"]),
            sorted(["action", "target_id", "record", "record_patch", "replacement_record", "tombstone", "pin"]),
        )

    def test_mcp_stdio_server_smoke(self) -> None:
        server = subprocess.run(
            [
                str(ROOT / "bin" / "memory-mcp"),
                "--cwd",
                str(self.workspace),
                "--memory-home",
                str(self.memory_home),
            ],
            input="\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "unit-test", "version": "1.0"},
                            },
                        }
                    ),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {"name": "memory.get_context", "arguments": {}},
                        }
                    ),
                    "",
                ]
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(server.returncode, 0, server.stderr)
        responses = [json.loads(line) for line in server.stdout.splitlines() if line.strip()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "memory-mcp")
        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertIn("memory.get_context", tool_names)
        content_text = responses[2]["result"]["content"][0]["text"]
        self.assertIn("rendered_text", content_text)

    def _run_admin(self, argv: list[str]) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            admin_main(argv)
        return output.getvalue()

    def _record_id_by_subject(self, scope: str, subject: str) -> str:
        if scope == GLOBAL_SCOPE:
            document = load_document(self.config.global_memory_path, GLOBAL_SCOPE)
        else:
            document = load_document(next(self.config.recent_dir.glob("*.md")), LOCAL_RECENT_SCOPE)
        for record in all_records(document):
            if record.subject == subject:
                return record.id
        raise AssertionError(f"record with subject {subject!r} not found in scope {scope}")

    def _write_env(self, lines: list[str]) -> Path:
        env_file = self.memory_home / ".env"
        env_file.write_text("\n".join([*lines, ""]), encoding="utf-8")
        return env_file


def _dt(raw: str):
    from datetime import UTC, datetime

    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


if __name__ == "__main__":
    unittest.main()
