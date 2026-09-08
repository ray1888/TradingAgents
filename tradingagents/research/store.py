"""SQLite persistence for research runs."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradingagents.research.protocol import STATUS_QUEUED

_CREATE = """
CREATE TABLE IF NOT EXISTS research_runs (
    run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    research_type TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    input_summary TEXT,
    result_json TEXT,
    pid INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_runs_idempotency
    ON research_runs(idempotency_key);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class IdempotencyConflict(ValueError):
    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"idempotency key already used by run {run_id}")


@dataclass
class ResearchRun:
    run_id: str
    schema_version: str
    research_type: str
    bundle_id: str
    idempotency_key: str
    request_hash: str
    status: str
    retryable: bool
    error: str | None
    input_summary: dict[str, Any]
    result: dict[str, Any] | None
    pid: int | None
    created_at: str
    updated_at: str

    def public_status(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "research_type": self.research_type,
            "bundle_id": self.bundle_id,
            "status": self.status,
            "retryable": self.retryable,
            "error": self.error,
            "input_summary": self.input_summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row(row: sqlite3.Row) -> ResearchRun:
    result_raw = row["result_json"]
    return ResearchRun(
        run_id=row["run_id"],
        schema_version=row["schema_version"],
        research_type=row["research_type"],
        bundle_id=row["bundle_id"],
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        status=row["status"],
        retryable=bool(row["retryable"]),
        error=row["error"],
        input_summary=json.loads(row["input_summary"] or "{}"),
        result=json.loads(result_raw) if result_raw else None,
        pid=row["pid"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ResearchRunStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_CREATE)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def create_or_get(
        self,
        *,
        run_id: str,
        schema_version: str,
        research_type: str,
        bundle_id: str,
        idempotency_key: str,
        request_hash: str,
        input_summary: dict[str, Any],
    ) -> ResearchRun:
        now = _now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM research_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                run = _row(existing)
                if run.request_hash != request_hash:
                    raise IdempotencyConflict(run.run_id)
                return run
            conn.execute(
                """
                INSERT INTO research_runs (
                    run_id, schema_version, research_type, bundle_id, idempotency_key,
                    request_hash, status, retryable, error, input_summary, result_json,
                    pid, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    schema_version,
                    research_type,
                    bundle_id,
                    idempotency_key,
                    request_hash,
                    STATUS_QUEUED,
                    json.dumps(input_summary, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
            return _row(
                conn.execute(
                    "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )

    def get(self, run_id: str) -> ResearchRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row(row) if row else None

    def list_queued(self) -> list[ResearchRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_runs WHERE status = ? ORDER BY created_at ASC",
                (STATUS_QUEUED,),
            ).fetchall()
        return [_row(row) for row in rows]

    def list_running(self) -> list[ResearchRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_runs WHERE status = 'running'"
            ).fetchall()
        return [_row(row) for row in rows]

    def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        retryable: bool | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        pid: int | None = None,
        clear_pid: bool = False,
    ) -> ResearchRun:
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        next_status = status if status is not None else run.status
        next_retryable = run.retryable if retryable is None else retryable
        next_error = run.error if error is None else error
        next_result = run.result if result is None else result
        next_pid = None if clear_pid else (pid if pid is not None else run.pid)
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE research_runs
                SET status = ?, retryable = ?, error = ?, result_json = ?, pid = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    next_status,
                    int(next_retryable),
                    next_error,
                    json.dumps(next_result, ensure_ascii=False) if next_result is not None else None,
                    next_pid,
                    now,
                    run_id,
                ),
            )
            conn.commit()
        updated = self.get(run_id)
        assert updated is not None
        return updated
