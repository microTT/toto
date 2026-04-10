from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import sqlite3
import subprocess
from pathlib import Path
from urllib import request as urllib_request
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_system.admin import main as admin_main
from memory_system.archive import archive_stale_recent_documents
from memory_system.bootstrap import ensure_layout
from memory_system.config import MemoryConfig, compute_workspace_identity, resolve_config
from memory_system.constants import GLOBAL_SCOPE, LOCAL_RECENT_SCOPE, STATUS_ACTIVE, STATUS_OPEN, STATUS_SUPERSEDED
from memory_system.embedding import embed_document_text, embed_query_text, load_embedding_settings
from memory_system.errors import PatchApplyError, SummarizerExecutionError
from memory_system.hooks import run_hook
from memory_system.markdown_store import all_records, empty_document, load_document, save_document
from memory_system.models import MemoryRecord
from memory_system.patch_applier import apply_patch_plan, current_base_revisions
from memory_system.record_store import find_record
from memory_system.search_index import SearchIndex, search_old_records
from memory_system.snapshot import build_snapshot
from memory_system.state_db import StateDB, SummaryJob
from memory_system.summarizer import build_patch_prompt, load_summarizer_settings, summarize_job
from memory_system.web_service import (
    build_workspace_detail_payload,
    create_server,
)
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

    def test_search_old_handles_punctuation_heavy_queries(self) -> None:
        patch_plan = {
            "decision": "write",
            "reason": "seed archive search punctuation case",
            "base_revisions": current_base_revisions(self.config),
            "global_ops": [],
            "local_ops": [
                {
                    "action": "create",
                    "record": {
                        "id": "l_old_ssh_details",
                        "type": "task_context",
                        "status": "closed",
                        "confidence": "high",
                        "subject": "ssh connection details",
                        "summary": "Use aws-mm.pem to SSH into 13.217.101.74 as ubuntu or ec2-user.",
                        "tags": ["aws", "ssh", "ec2-user"],
                        "source_refs": [],
                        "scope_reason": "repo-specific and near-term",
                    },
                }
            ],
            "needs_manual_review": False,
        }
        apply_patch_plan(self.config, patch_plan, now=_dt("2020-01-01T10:00:00Z"))
        archive_stale_recent_documents(self.config, now=_dt("2020-01-03T10:00:00Z"))

        index = SearchIndex(self.config.index_db_path)
        try:
            index.rebuild(self.config)
            results = index.search_old(
                workspace_instance_id=self.config.workspace_instance_id,
                query="ladder AWS EC2 SSH host username pem 13.217.101.74 aws-mm.pem ubuntu ec2-user",
                top_k=5,
            )
        finally:
            index.close()

        self.assertEqual(results[0]["record_id"], "l_old_ssh_details")

    def test_same_repo_search_federates_across_peer_memory_homes(self) -> None:
        peer_workspace = Path(self.temp_dir.name) / "workspace-peer"
        peer_workspace.mkdir(parents=True, exist_ok=True)
        peer_identity = compute_workspace_identity(peer_workspace)
        peer_config = MemoryConfig(
            memory_home=Path(self.temp_dir.name) / peer_identity.workspace_instance_id,
            workspace_root=peer_identity.workspace_root,
            cwd=peer_identity.workspace_root,
            repo_id=self.config.repo_id,
            workspace_instance_id=peer_identity.workspace_instance_id,
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
        self.assertEqual(same_repo_results[0]["workspace_instance_id"], peer_identity.workspace_instance_id)

    def test_web_service_workspace_detail_includes_global_recent_archive_and_snapshot(self) -> None:
        self._run_admin(
            [
                "--cwd",
                str(self.workspace),
                "upsert",
                "--scope",
                "global",
                "--id",
                "g_launch_pref",
                "--type",
                "preference",
                "--subject",
                "launch agent preference",
                "--summary",
                "Prefer running the memory viewer as a local service.",
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
                "--id",
                "l_recent_follow_up",
                "--type",
                "task_context",
                "--subject",
                "web viewer follow-up",
                "--summary",
                "Polish the memory viewer UI after the API lands.",
                "--scope-reason",
                "repo-specific and near-term",
            ]
        )
        archived_plan = {
            "decision": "write",
            "reason": "seed archived record for viewer",
            "base_revisions": current_base_revisions(self.config),
            "global_ops": [],
            "local_ops": [
                {
                    "action": "create",
                    "record": {
                        "id": "l_archived_memory",
                        "type": "failed_attempt",
                        "status": "closed",
                        "confidence": "high",
                        "subject": "old archive note",
                        "summary": "Archive this older failed attempt for the viewer.",
                        "tags": ["archive"],
                        "source_refs": [],
                        "scope_reason": "repo-specific and near-term",
                    },
                }
            ],
            "needs_manual_review": False,
        }
        apply_patch_plan(self.config, archived_plan, now=_dt("2020-01-01T10:00:00Z"))
        archive_stale_recent_documents(self.config, now=_dt("2020-01-03T10:00:00Z"))

        snapshot_path = self.config.runtime_dir / "session_test-runtime.json"
        snapshot_payload = build_snapshot(self.config).to_dict()
        snapshot_payload["built_at"] = "2026-04-09T01:02:03Z"
        snapshot_path.write_text(json.dumps(snapshot_payload, ensure_ascii=False), encoding="utf-8")

        detail = build_workspace_detail_payload(self.config, current_config=self.config)
        record_ids = {record["id"] for record in detail["records"]}

        self.assertIn("g_launch_pref", record_ids)
        self.assertIn("l_recent_follow_up", record_ids)
        self.assertIn("l_archived_memory", record_ids)
        self.assertEqual(detail["snapshot"]["source"], "runtime")
        self.assertEqual(detail["snapshot"]["session_id"], "test-runtime")
        self.assertEqual(detail["workspace"]["counts"]["global"], 1)
        self.assertGreaterEqual(detail["workspace"]["counts"]["recent"], 1)
        self.assertGreaterEqual(detail["workspace"]["counts"]["archive"], 1)

    def test_web_service_http_api_serves_health_and_workspace_index(self) -> None:
        peer_workspace = Path(self.temp_dir.name) / "workspace-peer"
        peer_workspace.mkdir(parents=True, exist_ok=True)
        peer_identity = compute_workspace_identity(peer_workspace)
        peer_config = MemoryConfig(
            memory_home=Path(self.temp_dir.name) / peer_identity.workspace_instance_id,
            workspace_root=peer_identity.workspace_root,
            cwd=peer_identity.workspace_root,
            repo_id=peer_identity.repo_id,
            workspace_instance_id=peer_identity.workspace_instance_id,
        )
        ensure_layout(peer_config)
        peer_document = empty_document(
            LOCAL_RECENT_SCOPE,
            path=peer_config.recent_dir / "2026-04-09.md",
            metadata={
                "repo_id": peer_config.repo_id,
                "workspace_instance_id": peer_config.workspace_instance_id,
                "workspace_root": str(peer_config.workspace_root),
                "date": "2026-04-09",
            },
        )
        peer_document.sections["Open"].append(
            MemoryRecord(
                id="l_peer_viewer",
                type="fact",
                status="open",
                confidence="high",
                subject="peer workspace memory",
                summary="Peer workspace should appear in the viewer index.",
                scope_reason="repo-specific and near-term",
            )
        )
        save_document(peer_config.recent_dir / "2026-04-09.md", peer_document)

        server = create_server(self.config, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, 1)
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        with urllib_request.urlopen(f"{base_url}/api/health") as response:
            health = json.loads(response.read().decode("utf-8"))
        with urllib_request.urlopen(f"{base_url}/api/workspaces") as response:
            workspace_index = json.loads(response.read().decode("utf-8"))

        workspace_ids = {item["workspace_instance_id"] for item in workspace_index["workspaces"]}
        self.assertTrue(health["ok"])
        self.assertEqual(health["workspace_instance_id"], self.config.workspace_instance_id)
        self.assertIn(self.config.workspace_instance_id, workspace_ids)
        self.assertIn(peer_identity.workspace_instance_id, workspace_ids)

    def test_snapshot_ignores_foreign_recent_documents_in_shared_memory_home(self) -> None:
        foreign_workspace = Path(self.temp_dir.name) / "workspace-foreign"
        foreign_workspace.mkdir(parents=True, exist_ok=True)
        foreign_config = resolve_config(foreign_workspace)
        ensure_layout(foreign_config)
        foreign_path = self.config.recent_dir / "peer.md"
        foreign_document = empty_document(
            LOCAL_RECENT_SCOPE,
            path=foreign_path,
            metadata={
                "repo_id": foreign_config.repo_id,
                "workspace_instance_id": foreign_config.workspace_instance_id,
                "workspace_root": str(foreign_config.workspace_root),
                "date": "2026-04-08",
            },
        )
        foreign_document.sections["Open"].append(
            MemoryRecord(
                id="l_foreign_recent",
                type="task_context",
                status="open",
                confidence="high",
                subject="foreign workspace note",
                summary="This record should not appear in the current workspace snapshot.",
                scope_reason="repo-specific and near-term",
            )
        )
        save_document(foreign_path, foreign_document)

        snapshot = build_snapshot(self.config, now=_dt("2026-04-08T12:00:00Z"))
        self.assertNotIn("foreign workspace note", snapshot.rendered_text)

    def test_search_index_uses_document_metadata_in_shared_memory_home(self) -> None:
        foreign_workspace = Path(self.temp_dir.name) / "workspace-foreign"
        foreign_workspace.mkdir(parents=True, exist_ok=True)
        foreign_config = resolve_config(foreign_workspace)
        ensure_layout(foreign_config)
        archive_path = self.config.archive_dir / "2020" / "01" / "foreign.md"
        archive_document = empty_document(
            "local_archive",
            path=archive_path,
            metadata={
                "repo_id": foreign_config.repo_id,
                "workspace_instance_id": foreign_config.workspace_instance_id,
                "workspace_root": str(foreign_config.workspace_root),
                "date": "2020-01-01",
            },
        )
        archive_document.sections["Closed"].append(
            MemoryRecord(
                id="l_foreign_archive",
                type="failed_attempt",
                status="closed",
                confidence="high",
                subject="foreign archive entry",
                summary="Only the foreign workspace should be able to search this record.",
                scope_reason="repo-specific and near-term",
            )
        )
        save_document(archive_path, archive_document)

        index = SearchIndex(self.config.index_db_path)
        try:
            index.rebuild(self.config)
            current_results = index.search_old(
                workspace_instance_id=self.config.workspace_instance_id,
                query="foreign archive entry",
                top_k=5,
            )
            foreign_results = index.search_old(
                workspace_instance_id=foreign_config.workspace_instance_id,
                query="foreign archive entry",
                top_k=5,
            )
        finally:
            index.close()

        self.assertEqual(current_results, [])
        self.assertEqual(foreign_results[0]["record_id"], "l_foreign_archive")

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

    def test_hook_to_worker_flow_writes_explicit_long_term_preference_to_global(self) -> None:
        run_hook(
            "session-start",
            {
                "session_id": "session-global",
                "turn_id": "turn-0",
                "cwd": str(self.workspace),
            },
        )
        stop_result = run_hook(
            "stop",
            {
                "session_id": "session-global",
                "turn_id": "turn-1",
                "cwd": str(self.workspace),
                "user_message_delta": "记住，你以后叫 思绎。",
                "assistant_message_delta": "好，我会记住。",
            },
        )
        self.assertEqual(stop_result, "")
        worker_result = run_worker_once(str(self.workspace), backend="heuristic")
        self.assertTrue(worker_result["applied"])

        global_document = load_document(self.config.global_memory_path, GLOBAL_SCOPE)
        record = next(record for record in all_records(global_document) if record.subject == "Assistant name preference")
        self.assertEqual(record.status, STATUS_ACTIVE)
        self.assertIn("思绎", record.summary)

    def test_build_patch_prompt_includes_cross_workspace_evidence_for_explicit_global_candidate(self) -> None:
        peer_workspace = Path(self.temp_dir.name) / "workspace-peer"
        peer_workspace.mkdir(parents=True, exist_ok=True)
        peer_identity = compute_workspace_identity(peer_workspace)
        peer_config = MemoryConfig(
            memory_home=Path(self.temp_dir.name) / peer_identity.workspace_instance_id,
            workspace_root=peer_identity.workspace_root,
            cwd=peer_identity.workspace_root,
            repo_id=peer_identity.repo_id,
            workspace_instance_id=peer_identity.workspace_instance_id,
        )
        ensure_layout(peer_config)
        apply_patch_plan(
            peer_config,
            {
                "decision": "write",
                "reason": "seed peer global preference",
                "base_revisions": current_base_revisions(peer_config),
                "global_ops": [
                    {
                        "action": "create",
                        "record": {
                            "id": "g_peer_pkg_pref",
                            "type": "preference",
                            "status": "active",
                            "confidence": "high",
                            "subject": "package manager preference",
                            "summary": "Future JavaScript/TypeScript repositories should default to pnpm unless the repository explicitly requires another package manager.",
                            "tags": ["tooling", "package-manager", "pnpm"],
                            "source_refs": [],
                            "scope_reason": "cross-workspace and durable",
                        },
                    }
                ],
                "local_ops": [],
                "needs_manual_review": False,
            },
        )
        apply_patch_plan(
            peer_config,
            {
                "decision": "write",
                "reason": "seed peer workspace identity",
                "base_revisions": current_base_revisions(peer_config),
                "global_ops": [],
                "local_ops": [
                    {
                        "action": "create",
                        "record": {
                            "id": "l_peer_workspace_probe",
                            "type": "task_context",
                            "status": "open",
                            "confidence": "high",
                            "subject": "peer workspace probe",
                            "summary": "Allow peer workspace discovery for prompt evidence tests.",
                            "tags": [],
                            "source_refs": [],
                            "scope_reason": "repo-specific and near-term",
                        },
                    }
                ],
                "needs_manual_review": False,
            },
        )

        job = SummaryJob(
            id=99,
            job_key="job-prompt-evidence",
            session_id="session-prompt-evidence",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )
        prompt = build_patch_prompt(
            config=self.config,
            job=job,
            events=[{"user_message_delta": "记住，我以后默认用 pnpm。", "assistant_message_delta": "收到。"}],
        )
        self.assertIn('"subject": "package manager preference"', prompt)
        self.assertIn('"global_hit_count": 1', prompt)
        self.assertIn(peer_identity.workspace_instance_id, prompt)

    def test_worker_once_scans_peer_memory_homes(self) -> None:
        peer_workspace = Path(self.temp_dir.name) / "workspace-peer"
        peer_workspace.mkdir(parents=True, exist_ok=True)
        peer_identity = compute_workspace_identity(peer_workspace)
        peer_memory_home = Path(self.temp_dir.name) / peer_identity.workspace_instance_id
        with mock.patch.dict(os.environ, {"CODEX_MEMORY_HOME": str(peer_memory_home)}, clear=False):
            peer_config = resolve_config(peer_workspace)
            ensure_layout(peer_config)
            run_hook(
                "session-start",
                {
                    "session_id": "peer-session",
                    "turn_id": "turn-0",
                    "cwd": str(peer_workspace),
                },
            )
            run_hook(
                "stop",
                {
                    "session_id": "peer-session",
                    "turn_id": "turn-1",
                    "cwd": str(peer_workspace),
                    "user_message_delta": "remember next step: peer workspace follow-up",
                    "assistant_message_delta": "Will keep that peer task in mind.",
                },
            )

        worker_result = run_worker_once(str(self.workspace), memory_home=str(self.memory_home), backend="heuristic")
        self.assertTrue(worker_result["applied"])
        self.assertEqual(Path(worker_result["workspace_root"]), peer_workspace.resolve())

        peer_snapshot = build_snapshot(peer_config)
        self.assertIn("peer workspace follow-up", peer_snapshot.rendered_text)

    def test_repair_mixed_store_moves_foreign_records_by_source_ref(self) -> None:
        foreign_workspace = Path(self.temp_dir.name) / "workspace-foreign"
        foreign_workspace.mkdir(parents=True, exist_ok=True)
        foreign_identity = compute_workspace_identity(foreign_workspace)
        foreign_config = MemoryConfig(
            memory_home=Path(self.temp_dir.name) / foreign_identity.workspace_instance_id,
            workspace_root=foreign_identity.workspace_root,
            cwd=foreign_identity.workspace_root,
            repo_id=foreign_identity.repo_id,
            workspace_instance_id=foreign_identity.workspace_instance_id,
        )
        ensure_layout(foreign_config)
        mixed_path = self.config.recent_dir / "2026-04-08.md"
        mixed_document = empty_document(
            LOCAL_RECENT_SCOPE,
            path=mixed_path,
            metadata={
                "repo_id": self.config.repo_id,
                "workspace_instance_id": self.config.workspace_instance_id,
                "workspace_root": str(self.config.workspace_root),
                "date": "2026-04-08",
            },
        )
        source_ref = foreign_workspace / "README.md"
        mixed_document.sections["Open"].append(
            MemoryRecord(
                id="l_foreign_source_ref",
                type="task_context",
                status="open",
                confidence="high",
                subject="foreign source ref record",
                summary="This record belongs to the foreign workspace.",
                source_refs=[str(source_ref)],
                scope_reason="repo-specific and near-term",
            )
        )
        save_document(mixed_path, mixed_document)

        payload = json.loads(self._run_admin(["--cwd", str(self.workspace), "repair-mixed-store"]))
        moved_ids = {item["record_id"] for item in payload["moved_records"]}
        self.assertIn("l_foreign_source_ref", moved_ids)

        current_snapshot = build_snapshot(self.config, now=_dt("2026-04-08T12:00:00Z"))
        self.assertNotIn("foreign source ref record", current_snapshot.rendered_text)

        foreign_snapshot = build_snapshot(foreign_config, now=_dt("2026-04-08T12:00:00Z"))
        self.assertIn("foreign source ref record", foreign_snapshot.rendered_text)

    def test_repair_mixed_store_uses_event_probes_for_non_repo_paths(self) -> None:
        foreign_workspace = Path(self.temp_dir.name) / "workspace-foreign"
        foreign_workspace.mkdir(parents=True, exist_ok=True)
        foreign_identity = compute_workspace_identity(foreign_workspace)
        foreign_config = MemoryConfig(
            memory_home=Path(self.temp_dir.name) / foreign_identity.workspace_instance_id,
            workspace_root=foreign_identity.workspace_root,
            cwd=foreign_identity.workspace_root,
            repo_id=foreign_identity.repo_id,
            workspace_instance_id=foreign_identity.workspace_instance_id,
        )
        ensure_layout(foreign_config)
        probe_path = Path(self.temp_dir.name) / "shared" / "tide.pem"
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        probe_path.write_text("pem", encoding="utf-8")

        state = StateDB(self.config.state_db_path)
        try:
            state.append_event(
                session_id="foreign-session",
                turn_id="turn-1",
                event_name="Stop",
                event_time="2026-04-08T03:46:29Z",
                cwd=str(foreign_workspace),
                transcript_path=None,
                user_message_delta=f"copy {probe_path} into the current repo and remove aws-mm.pem",
                assistant_message_delta=None,
                summary_cursor_before=None,
                summary_cursor_after=None,
                payload={},
            )
        finally:
            state.close()

        mixed_path = self.config.recent_dir / "2026-04-08.md"
        mixed_document = empty_document(
            LOCAL_RECENT_SCOPE,
            path=mixed_path,
            metadata={
                "repo_id": self.config.repo_id,
                "workspace_instance_id": self.config.workspace_instance_id,
                "workspace_root": str(self.config.workspace_root),
                "date": "2026-04-08",
            },
        )
        mixed_document.sections["Open"].append(
            MemoryRecord(
                id="l_foreign_event_match",
                type="task_context",
                status="open",
                confidence="high",
                subject="shared pem migration",
                summary=f"Copy {probe_path} into the current repo and update gitignore.",
                created_at="2026-04-08T03:46:49Z",
                updated_at="2026-04-08T03:46:49Z",
                scope_reason="repo-specific and near-term",
            )
        )
        save_document(mixed_path, mixed_document)

        payload = json.loads(self._run_admin(["--cwd", str(self.workspace), "repair-mixed-store"]))
        moved_ids = {item["record_id"] for item in payload["moved_records"]}
        self.assertIn("l_foreign_event_match", moved_ids)

        foreign_snapshot = build_snapshot(foreign_config, now=_dt("2026-04-08T12:00:00Z"))
        self.assertIn("shared pem migration", foreign_snapshot.rendered_text)

    def test_multiline_summary_round_trips_through_markdown_store(self) -> None:
        path = self.config.recent_dir / "2026-04-07.md"
        document = empty_document(
            LOCAL_RECENT_SCOPE,
            path=path,
            metadata={
                "repo_id": self.config.repo_id,
                "workspace_instance_id": self.config.workspace_instance_id,
                "workspace_root": str(self.config.workspace_root),
                "date": "2026-04-07",
            },
        )
        summary = "\n".join(
            [
                "为 agent 接入内网 ATA 搜索，推荐采用受控代理层架构：agent → 检索网关 → ATA。",
                "- 个人助手类 agent：用户登录后显式授权，网关代发短期 token。",
                "- 平台级 agent：使用服务账号 + 网关代理。",
                "关键原则：凭证短期有效、权限最小化、全链路审计。",
            ]
        )
        record = MemoryRecord(
            id="l_multiline_summary",
            type="task_context",
            status="open",
            confidence="high",
            subject="内网 ATA 搜索的 agent 授权方案",
            summary=summary,
            scope_reason="repo-specific and near-term",
        )
        document.sections["Open"].append(record)
        save_document(path, document)

        loaded = load_document(path, LOCAL_RECENT_SCOPE)
        loaded_record = next(record for record in all_records(loaded) if record.id == "l_multiline_summary")
        self.assertEqual(loaded_record.summary, summary)

    def test_parse_legacy_multiline_bullets_inside_summary(self) -> None:
        path = self.config.recent_dir / "2026-04-07.md"
        path.write_text(
            "\n".join(
                [
                    "---",
                    "date: 2026-04-07",
                    f"repo_id: {self.config.repo_id}",
                    "revision: 18",
                    "schema_version: 1",
                    "scope: local_recent",
                    "updated_at: 2026-04-07T08:42:50Z",
                    f"workspace_instance_id: {self.config.workspace_instance_id}",
                    f"workspace_root: {self.config.workspace_root}",
                    "---",
                    "",
                    "# Local Memory - 2026-04-07",
                    "",
                    "## Open",
                    "",
                    "### l_legacy_multiline_summary",
                    "- type: task_context",
                    "- status: open",
                    "- confidence: high",
                    "- subject: 内网 ATA 搜索的 agent 授权方案",
                    "- summary: 为 agent 接入内网 ATA 搜索，推荐采用受控代理层架构：agent → 检索网关 → ATA。具体建议：",
                    "- 个人助手类 agent：用户登录后显式授权，网关代发短期 token，高危操作需 HITL。",
                    "- 平台级 agent：使用服务账号 + 网关代理，权限最小化。",
                    "关键原则：凭证短期有效、权限最小化、全链路审计。",
                    '- tags: ["ata", "agent", "authorization"]',
                    "- created_at: 2026-04-07T08:27:11Z",
                    "- updated_at: 2026-04-07T08:42:50Z",
                    "- scope_reason: repo-specific and near-term",
                    "",
                    "## Active",
                    "",
                    "## Closed",
                    "",
                    "## Superseded",
                    "",
                    "## Deleted",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        loaded = load_document(path, LOCAL_RECENT_SCOPE)
        loaded_record = next(record for record in all_records(loaded) if record.id == "l_legacy_multiline_summary")
        self.assertEqual(
            loaded_record.summary,
            "\n".join(
                [
                    "为 agent 接入内网 ATA 搜索，推荐采用受控代理层架构：agent → 检索网关 → ATA。具体建议：",
                    "- 个人助手类 agent：用户登录后显式授权，网关代发短期 token，高危操作需 HITL。",
                    "- 平台级 agent：使用服务账号 + 网关代理，权限最小化。",
                    "关键原则：凭证短期有效、权限最小化、全链路审计。",
                ]
            ),
        )
        self.assertEqual(loaded_record.tags, ["ata", "agent", "authorization"])

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
        transient_error = SummarizerExecutionError("temporary qwen backend failure")
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
                backend="qwen",
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
            self.assertIn("temporary qwen backend failure", row[2])

            second = run_worker_once(
                str(self.workspace),
                memory_home=str(self.memory_home),
                backend="qwen",
                retry_base_seconds=0,
            )
        self.assertTrue(second["applied"])
        snapshot = build_snapshot(self.config)
        self.assertIn("retry auth snapshot flow", snapshot.rendered_text)

    def test_qwen_summarizer_settings_load_from_dotenv_file(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://example.com/compatible-mode/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=from-dotenv",
                "CODEX_MEMORY_SUMMARIZER_MODEL=qwen3-max",
                "CODEX_MEMORY_SUMMARIZER_ENDPOINT_MODE=openai",
                "CODEX_MEMORY_SUMMARIZER_TIMEOUT_SECONDS=90",
                "CODEX_MEMORY_SUMMARIZER_TEMPERATURE=0.2",
                "CODEX_MEMORY_SUMMARIZER_MAX_OUTPUT_TOKENS=2048",
            ]
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            settings = load_summarizer_settings(env_file=env_file)

        self.assertEqual(settings.provider, "qwen_openai")
        self.assertEqual(settings.base_url, "https://example.com/compatible-mode/v1")
        self.assertEqual(settings.api_key, "from-dotenv")
        self.assertEqual(settings.model_name, "qwen3-max")
        self.assertEqual(settings.endpoint_mode, "openai")
        self.assertEqual(settings.timeout_seconds, 90)
        self.assertAlmostEqual(settings.temperature, 0.2)
        self.assertEqual(settings.max_output_tokens, 2048)

    def test_environment_overrides_dotenv_summarizer_settings(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://dotenv.example/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=from-dotenv",
                "CODEX_MEMORY_SUMMARIZER_MODEL=qwen3-max",
                "CODEX_MEMORY_SUMMARIZER_TIMEOUT_SECONDS=90",
            ]
        )

        with mock.patch.dict(
            os.environ,
            {
                "CODEX_MEMORY_SUMMARIZER_PROVIDER": "auto",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL": "https://env.example/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY": "from-env",
                "CODEX_MEMORY_SUMMARIZER_MODEL": "qwen3-max-preview",
                "CODEX_MEMORY_SUMMARIZER_TIMEOUT_SECONDS": "45",
            },
            clear=True,
        ):
            settings = load_summarizer_settings(env_file=env_file)

        self.assertEqual(settings.provider, "auto")
        self.assertEqual(settings.base_url, "https://env.example/v1")
        self.assertEqual(settings.api_key, "from-env")
        self.assertEqual(settings.model_name, "qwen3-max-preview")
        self.assertEqual(settings.timeout_seconds, 45)

    def test_summarizer_settings_can_reuse_embedding_connection_defaults(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_EMBEDDING_BASE_URL=https://embedding.example/v1",
                "CODEX_MEMORY_EMBEDDING_API_KEY=shared-token",
            ]
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            settings = load_summarizer_settings(env_file=env_file)

        self.assertEqual(settings.base_url, "https://embedding.example/v1")
        self.assertEqual(settings.api_key, "shared-token")
        self.assertEqual(settings.model_name, "qwen3-max")

    def test_qwen_summarizer_uses_openai_compatible_chat_api(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://example.com/compatible-mode/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=test-token",
                "CODEX_MEMORY_SUMMARIZER_MODEL=qwen3-max",
                "CODEX_MEMORY_SUMMARIZER_ENDPOINT_MODE=openai",
                "CODEX_MEMORY_SUMMARIZER_TIMEOUT_SECONDS=30",
                "CODEX_MEMORY_SUMMARIZER_MAX_OUTPUT_TOKENS=1024",
            ]
        )
        captured_requests: list[tuple[str, dict[str, object], dict[str, str], int]] = []
        response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "write",
                                "reason": "detected explicit next step",
                                "base_revisions": current_base_revisions(self.config),
                                "global_ops": [],
                                "local_ops": [
                                    {
                                        "action": "create",
                                        "record": {
                                            "type": "task_context",
                                            "status": "open",
                                            "confidence": "high",
                                            "subject": "下一步",
                                            "summary": "重新检查失败的 auth 快照",
                                            "tags": ["todo", "auth"],
                                            "source_refs": ["transcript_delta"],
                                            "scope_reason": "repo-specific and near-term",
                                        },
                                    }
                                ],
                                "needs_manual_review": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return response_payload

        def fake_post(url, json=None, headers=None, timeout=None):
            captured_requests.append((url, json, headers, timeout))
            return FakeResponse()

        job = SummaryJob(
            id=1,
            job_key="job-key",
            session_id="session-qwen",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )

        with mock.patch.dict(os.environ, {"CODEX_MEMORY_ENV_FILE": str(env_file)}, clear=False), mock.patch(
            "requests.post",
            side_effect=fake_post,
        ):
            patch_plan = summarize_job(
                config=self.config,
                job=job,
                events=[{"user_message_delta": "记住下一步：重新检查失败的 auth 快照", "assistant_message_delta": "好"}],
                backend="qwen",
            )

        self.assertEqual(patch_plan["decision"], "write")
        self.assertEqual(captured_requests[0][0], "https://example.com/compatible-mode/v1/chat/completions")
        self.assertEqual(captured_requests[0][2]["Authorization"], "Bearer test-token")
        self.assertEqual(captured_requests[0][3], 30)
        self.assertEqual(captured_requests[0][1]["model"], "qwen3-max")
        self.assertEqual(captured_requests[0][1]["max_tokens"], 1024)
        messages = captured_requests[0][1]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("记住下一步", messages[1]["content"])

    def test_qwen_summarizer_short_circuits_explicit_global_memory_request(self) -> None:
        job = SummaryJob(
            id=8,
            job_key="job-key-8",
            session_id="session-qwen-8",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )

        with mock.patch("requests.post") as mocked_post:
            patch_plan = summarize_job(
                config=self.config,
                job=job,
                events=[{"user_message_delta": "记住，你以后叫 思绎。", "assistant_message_delta": "好。"}],
                backend="qwen",
            )

        self.assertEqual(patch_plan["decision"], "write")
        self.assertEqual(patch_plan["global_ops"][0]["record"]["subject"], "Assistant name preference")
        self.assertIn("思绎", patch_plan["global_ops"][0]["record"]["summary"])
        mocked_post.assert_not_called()

    def test_qwen_summarizer_normalizes_common_alias_fields(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://example.com/compatible-mode/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=test-token",
            ]
        )
        response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "update",
                                "reason": "refresh existing task",
                                "base_revisions": current_base_revisions(self.config),
                                "global_ops": [],
                                "local_ops": [
                                    {
                                        "action": "update",
                                        "id": "l_existing_task",
                                        "fields": {"status": "open", "updated_at": "2026-04-02T12:47:36Z"},
                                    }
                                ],
                                "needs_manual_review": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return response_payload

        job = SummaryJob(
            id=2,
            job_key="job-key-2",
            session_id="session-qwen-2",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )

        with mock.patch.dict(os.environ, {"CODEX_MEMORY_ENV_FILE": str(env_file)}, clear=False), mock.patch(
            "requests.post",
            return_value=FakeResponse(),
        ):
            patch_plan = summarize_job(
                config=self.config,
                job=job,
                events=[{"user_message_delta": "记住下一步：重新检查失败的 auth 快照", "assistant_message_delta": "好"}],
                backend="qwen",
            )

        self.assertEqual(patch_plan["decision"], "write")
        self.assertEqual(patch_plan["local_ops"][0]["target_id"], "l_existing_task")
        self.assertEqual(patch_plan["local_ops"][0]["record_patch"]["status"], "open")

    def test_qwen_summarizer_repairs_content_only_create_ops(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://example.com/compatible-mode/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=test-token",
            ]
        )
        response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "apply",
                                "reason": "remember the next step",
                                "base_revisions": current_base_revisions(self.config),
                                "global_ops": [],
                                "local_ops": [
                                    {
                                        "action": "create",
                                        "content": "重新检查失败的 auth 快照",
                                    }
                                ],
                                "needs_manual_review": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return response_payload

        job = SummaryJob(
            id=3,
            job_key="job-key-3",
            session_id="session-qwen-3",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )

        with mock.patch.dict(os.environ, {"CODEX_MEMORY_ENV_FILE": str(env_file)}, clear=False), mock.patch(
            "requests.post",
            return_value=FakeResponse(),
        ):
            patch_plan = summarize_job(
                config=self.config,
                job=job,
                events=[{"user_message_delta": "记住下一步：重新检查失败的 auth 快照", "assistant_message_delta": "好"}],
                backend="qwen",
            )

        self.assertEqual(patch_plan["decision"], "write")
        record = patch_plan["local_ops"][0]["record"]
        self.assertEqual(record["type"], "task_context")
        self.assertEqual(record["status"], "open")
        self.assertEqual(record["summary"], "重新检查失败的 auth 快照")
        self.assertEqual(record["subject"], "重新检查失败的 auth 快照")

    def test_qwen_summarizer_repairs_nested_record_content_aliases(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://example.com/compatible-mode/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=test-token",
            ]
        )
        response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "update",
                                "reason": "remember the next step",
                                "base_revisions": current_base_revisions(self.config),
                                "global_ops": [],
                                "local_ops": [
                                    {
                                        "action": "create",
                                        "record": {
                                            "type": "task_context",
                                            "status": "open",
                                            "content": "重新检查失败的 auth 快照",
                                        },
                                    }
                                ],
                                "needs_manual_review": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return response_payload

        job = SummaryJob(
            id=5,
            job_key="job-key-5",
            session_id="session-qwen-5",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )

        with mock.patch.dict(os.environ, {"CODEX_MEMORY_ENV_FILE": str(env_file)}, clear=False), mock.patch(
            "requests.post",
            return_value=FakeResponse(),
        ):
            patch_plan = summarize_job(
                config=self.config,
                job=job,
                events=[{"user_message_delta": "记住下一步：重新检查失败的 auth 快照", "assistant_message_delta": "好"}],
                backend="qwen",
            )

        record = patch_plan["local_ops"][0]["record"]
        self.assertEqual(record["summary"], "重新检查失败的 auth 快照")
        self.assertEqual(record["subject"], "重新检查失败的 auth 快照")
        self.assertEqual(record["confidence"], "medium")
        self.assertEqual(record["scope_reason"], "repo-specific and near-term")

    def test_qwen_summarizer_rejects_irreparable_invalid_patch_plan(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://example.com/compatible-mode/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=test-token",
            ]
        )
        response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "apply",
                                "reason": "invalid create op",
                                "base_revisions": current_base_revisions(self.config),
                                "global_ops": [],
                                "local_ops": [{"action": "create"}],
                                "needs_manual_review": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return response_payload

        job = SummaryJob(
            id=4,
            job_key="job-key-4",
            session_id="session-qwen-4",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )

        with mock.patch.dict(os.environ, {"CODEX_MEMORY_ENV_FILE": str(env_file)}, clear=False), mock.patch(
            "requests.post",
            return_value=FakeResponse(),
        ):
            with self.assertRaises(SummarizerExecutionError):
                summarize_job(
                    config=self.config,
                    job=job,
                    events=[{"user_message_delta": "你好", "assistant_message_delta": "收到"}],
                    backend="qwen",
                )

    def test_qwen_summarizer_repairs_supersede_record_alias_and_status_alias(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://example.com/compatible-mode/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=test-token",
            ]
        )
        response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "apply",
                                "reason": "replace stale local task",
                                "base_revisions": current_base_revisions(self.config),
                                "global_ops": [],
                                "local_ops": [
                                    {
                                        "action": "supersede",
                                        "target_id": "l_existing_task",
                                        "record": {
                                            "type": "task_context",
                                            "status": "resolved",
                                            "content": "新的跟进结论",
                                        },
                                    }
                                ],
                                "needs_manual_review": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return response_payload

        job = SummaryJob(
            id=6,
            job_key="job-key-6",
            session_id="session-qwen-6",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )

        with mock.patch.dict(os.environ, {"CODEX_MEMORY_ENV_FILE": str(env_file)}, clear=False), mock.patch(
            "requests.post",
            return_value=FakeResponse(),
        ):
            patch_plan = summarize_job(
                config=self.config,
                job=job,
                events=[{"user_message_delta": "更新记忆", "assistant_message_delta": "收到"}],
                backend="qwen",
            )

        replacement = patch_plan["local_ops"][0]["replacement_record"]
        self.assertEqual(replacement["status"], "closed")
        self.assertEqual(replacement["summary"], "新的跟进结论")
        self.assertEqual(replacement["subject"], "新的跟进结论")

    def test_qwen_summarizer_repairs_update_record_alias(self) -> None:
        env_file = self._write_env(
            [
                "CODEX_MEMORY_SUMMARIZER_PROVIDER=qwen_openai",
                "CODEX_MEMORY_SUMMARIZER_BASE_URL=https://example.com/compatible-mode/v1",
                "CODEX_MEMORY_SUMMARIZER_API_KEY=test-token",
            ]
        )
        response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "update",
                                "reason": "refresh existing task",
                                "base_revisions": current_base_revisions(self.config),
                                "global_ops": [],
                                "local_ops": [
                                    {
                                        "action": "update",
                                        "id": "l_existing_task",
                                        "record": {
                                            "id": "l_existing_task",
                                            "status": "open",
                                            "content": "重新检查失败的 auth 快照",
                                        },
                                    }
                                ],
                                "needs_manual_review": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return response_payload

        job = SummaryJob(
            id=7,
            job_key="job-key-7",
            session_id="session-qwen-7",
            repo_id=self.config.repo_id,
            workspace_instance_id=self.config.workspace_instance_id,
            workspace_root=str(self.config.workspace_root),
            transcript_path=None,
            start_event_id=None,
            end_event_id=1,
            prompt_version="v1",
            reason="test",
            status="pending",
            attempt_count=0,
            max_attempts=3,
            next_attempt_at="1970-01-01T00:00:00Z",
            last_error=None,
            payload={},
            created_at="2026-04-02T00:00:00Z",
            updated_at="2026-04-02T00:00:00Z",
        )

        with mock.patch.dict(os.environ, {"CODEX_MEMORY_ENV_FILE": str(env_file)}, clear=False), mock.patch(
            "requests.post",
            return_value=FakeResponse(),
        ):
            patch_plan = summarize_job(
                config=self.config,
                job=job,
                events=[{"user_message_delta": "记住下一步：重新检查失败的 auth 快照", "assistant_message_delta": "好"}],
                backend="qwen",
            )

        record_patch = patch_plan["local_ops"][0]["record_patch"]
        self.assertEqual(record_patch["status"], "open")
        self.assertEqual(record_patch["summary"], "重新检查失败的 auth 快照")

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

    def test_patch_schema_is_flat_transport_schema(self) -> None:
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
