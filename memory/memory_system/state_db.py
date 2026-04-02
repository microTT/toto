from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .constants import DEFAULT_JOB_MAX_ATTEMPTS
from .utils import isoformat


@dataclass(slots=True)
class SummaryJob:
    id: int
    job_key: str
    session_id: str
    repo_id: str
    workspace_instance_id: str
    workspace_root: str
    transcript_path: str | None
    start_event_id: int | None
    end_event_id: int | None
    prompt_version: str
    reason: str
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: str
    last_error: str | None
    payload: dict[str, Any]
    created_at: str
    updated_at: str


class StateDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self) -> None:
        self.conn.close()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS session_snapshots (
              session_id TEXT PRIMARY KEY,
              repo_id TEXT NOT NULL,
              workspace_instance_id TEXT NOT NULL,
              workspace_root TEXT NOT NULL,
              snapshot_revision TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              built_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS summary_state (
              session_id TEXT PRIMARY KEY,
              last_event_id INTEGER,
              last_turn_id TEXT,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              turn_id TEXT NOT NULL,
              event_name TEXT NOT NULL,
              event_time TEXT NOT NULL,
              cwd TEXT,
              transcript_path TEXT,
              user_message_delta TEXT,
              assistant_message_delta TEXT,
              summary_cursor_before TEXT,
              summary_cursor_after TEXT,
              payload_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_unique
              ON events(session_id, turn_id, event_name);
            CREATE INDEX IF NOT EXISTS idx_events_session_id
              ON events(session_id, id);

            CREATE TABLE IF NOT EXISTS summary_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_key TEXT NOT NULL UNIQUE,
              session_id TEXT NOT NULL,
              repo_id TEXT NOT NULL,
              workspace_instance_id TEXT NOT NULL,
              workspace_root TEXT NOT NULL,
              transcript_path TEXT,
              start_event_id INTEGER,
              end_event_id INTEGER,
              prompt_version TEXT NOT NULL,
              reason TEXT NOT NULL,
              status TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL DEFAULT 3,
              next_attempt_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z',
              last_error TEXT,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_summary_jobs_status
              ON summary_jobs(status, created_at);
            """
        )
        self._ensure_column("summary_jobs", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("summary_jobs", "max_attempts", f"INTEGER NOT NULL DEFAULT {DEFAULT_JOB_MAX_ATTEMPTS}")
        self._ensure_column("summary_jobs", "next_attempt_at", "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z'")
        self._ensure_column("summary_jobs", "last_error", "TEXT")
        self.conn.commit()

    def save_session_snapshot(
        self,
        session_id: str,
        repo_id: str,
        workspace_instance_id: str,
        workspace_root: str,
        snapshot_revision: str,
        snapshot_json: dict[str, Any],
        built_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO session_snapshots(
              session_id, repo_id, workspace_instance_id, workspace_root,
              snapshot_revision, snapshot_json, built_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              repo_id = excluded.repo_id,
              workspace_instance_id = excluded.workspace_instance_id,
              workspace_root = excluded.workspace_root,
              snapshot_revision = excluded.snapshot_revision,
              snapshot_json = excluded.snapshot_json,
              built_at = excluded.built_at
            """,
            (
                session_id,
                repo_id,
                workspace_instance_id,
                workspace_root,
                snapshot_revision,
                json.dumps(snapshot_json, sort_keys=True),
                built_at,
            ),
        )
        self.conn.commit()

    def get_summary_cursor(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT last_event_id FROM summary_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None or row["last_event_id"] is None:
            return 0
        return int(row["last_event_id"])

    def update_summary_cursor(self, session_id: str, event_id: int | None, turn_id: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO summary_state(session_id, last_event_id, last_turn_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              last_event_id = excluded.last_event_id,
              last_turn_id = excluded.last_turn_id,
              updated_at = excluded.updated_at
            """,
            (session_id, event_id, turn_id, isoformat()),
        )
        self.conn.commit()

    def append_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        event_name: str,
        event_time: str,
        cwd: str | None,
        transcript_path: str | None,
        user_message_delta: str | None,
        assistant_message_delta: str | None,
        summary_cursor_before: str | None,
        summary_cursor_after: str | None,
        payload: dict[str, Any],
    ) -> int:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO events(
              session_id, turn_id, event_name, event_time, cwd, transcript_path,
              user_message_delta, assistant_message_delta, summary_cursor_before,
              summary_cursor_after, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn_id,
                event_name,
                event_time,
                cwd,
                transcript_path,
                user_message_delta,
                assistant_message_delta,
                summary_cursor_before,
                summary_cursor_after,
                json.dumps(payload, sort_keys=True),
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM events WHERE session_id = ? AND turn_id = ? AND event_name = ?",
            (session_id, turn_id, event_name),
        ).fetchone()
        return int(row["id"])

    def get_events_since(self, session_id: str, after_event_id: int) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM events
            WHERE session_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (session_id, after_event_id),
        ).fetchall()
        return rows

    def get_events_range(
        self, session_id: str, after_event_id: int, end_event_id: int | None
    ) -> list[sqlite3.Row]:
        if end_event_id is None:
            return self.get_events_since(session_id, after_event_id)
        rows = self.conn.execute(
            """
            SELECT *
            FROM events
            WHERE session_id = ? AND id > ? AND id <= ?
            ORDER BY id ASC
            """,
            (session_id, after_event_id, end_event_id),
        ).fetchall()
        return rows

    def upsert_summary_job(
        self,
        *,
        job_key: str,
        session_id: str,
        repo_id: str,
        workspace_instance_id: str,
        workspace_root: str,
        transcript_path: str | None,
        start_event_id: int | None,
        end_event_id: int | None,
        prompt_version: str,
        reason: str,
        payload: dict[str, Any],
        max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS,
    ) -> SummaryJob:
        now = isoformat()
        self.conn.execute(
            """
            INSERT INTO summary_jobs(
              job_key, session_id, repo_id, workspace_instance_id, workspace_root,
              transcript_path, start_event_id, end_event_id, prompt_version, reason,
              status, attempt_count, max_attempts, next_attempt_at, last_error,
              payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(job_key) DO UPDATE SET
              transcript_path = excluded.transcript_path,
              start_event_id = excluded.start_event_id,
              end_event_id = excluded.end_event_id,
              reason = excluded.reason,
              max_attempts = excluded.max_attempts,
              next_attempt_at = excluded.next_attempt_at,
              payload_json = excluded.payload_json,
              updated_at = excluded.updated_at
            """,
            (
                job_key,
                session_id,
                repo_id,
                workspace_instance_id,
                workspace_root,
                transcript_path,
                start_event_id,
                end_event_id,
                prompt_version,
                reason,
                max_attempts,
                now,
                json.dumps(payload, sort_keys=True),
                now,
                now,
            ),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM summary_jobs WHERE job_key = ?", (job_key,)).fetchone()
        return self._row_to_job(row)

    def fetch_next_pending_job(self, *, now: datetime | None = None) -> SummaryJob | None:
        threshold = isoformat(now)
        row = self.conn.execute(
            """
            SELECT *
            FROM summary_jobs
            WHERE status IN ('pending', 'retry_wait') AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, created_at ASC
            LIMIT 1
            """,
            (threshold,),
        ).fetchone()
        return None if row is None else self._row_to_job(row)

    def update_job_status(self, job_id: int, status: str, payload: dict[str, Any] | None = None) -> None:
        updated_payload = json.dumps(payload, sort_keys=True) if payload is not None else None
        if updated_payload is None:
            self.conn.execute(
                "UPDATE summary_jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, isoformat(), job_id),
            )
        else:
            self.conn.execute(
                "UPDATE summary_jobs SET status = ?, payload_json = ?, updated_at = ? WHERE id = ?",
                (status, updated_payload, isoformat(), job_id),
        )
        self.conn.commit()

    def mark_job_running(self, job_id: int) -> None:
        self.conn.execute(
            """
            UPDATE summary_jobs
            SET status = 'running',
                attempt_count = attempt_count + 1,
                updated_at = ?,
                last_error = NULL
            WHERE id = ?
            """,
            (isoformat(), job_id),
        )
        self.conn.commit()

    def schedule_job_retry(self, job_id: int, *, error: str, next_attempt_at: datetime) -> SummaryJob:
        self.conn.execute(
            """
            UPDATE summary_jobs
            SET status = 'retry_wait',
                next_attempt_at = ?,
                last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (isoformat(next_attempt_at), error, isoformat(), job_id),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM summary_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row)

    def mark_job_failed(self, job_id: int, *, error: str, payload: dict[str, Any] | None = None) -> None:
        updated_payload = json.dumps(payload, sort_keys=True) if payload is not None else None
        if updated_payload is None:
            self.conn.execute(
                """
                UPDATE summary_jobs
                SET status = 'failed',
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, isoformat(), job_id),
            )
        else:
            self.conn.execute(
                """
                UPDATE summary_jobs
                SET status = 'failed',
                    last_error = ?,
                    payload_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, updated_payload, isoformat(), job_id),
            )
        self.conn.commit()

    def requeue_stale_running_jobs(self, *, stale_before: datetime, retry_at: datetime) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT id
            FROM summary_jobs
            WHERE status = 'running' AND updated_at < ?
            """,
            (isoformat(stale_before),),
        ).fetchall()
        job_ids = [int(row["id"]) for row in rows]
        if not job_ids:
            return []
        self.conn.executemany(
            """
            UPDATE summary_jobs
            SET status = 'retry_wait',
                next_attempt_at = ?,
                last_error = COALESCE(last_error, 'requeued stale running job'),
                updated_at = ?
            WHERE id = ?
            """,
            [(isoformat(retry_at), isoformat(), job_id) for job_id in job_ids],
        )
        self.conn.commit()
        return job_ids

    def delete_finished_jobs(self, *, completed_before: datetime, failed_before: datetime) -> int:
        before_count = self.conn.total_changes
        self.conn.execute(
            """
            DELETE FROM summary_jobs
            WHERE (status = 'completed' AND updated_at < ?)
               OR (status = 'failed' AND updated_at < ?)
            """,
            (isoformat(completed_before), isoformat(failed_before)),
        )
        self.conn.commit()
        return self.conn.total_changes - before_count

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _row_to_job(self, row: sqlite3.Row) -> SummaryJob:
        return SummaryJob(
            id=int(row["id"]),
            job_key=str(row["job_key"]),
            session_id=str(row["session_id"]),
            repo_id=str(row["repo_id"]),
            workspace_instance_id=str(row["workspace_instance_id"]),
            workspace_root=str(row["workspace_root"]),
            transcript_path=row["transcript_path"],
            start_event_id=row["start_event_id"],
            end_event_id=row["end_event_id"],
            prompt_version=str(row["prompt_version"]),
            reason=str(row["reason"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=str(row["next_attempt_at"]),
            last_error=row["last_error"],
            payload=json.loads(row["payload_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
