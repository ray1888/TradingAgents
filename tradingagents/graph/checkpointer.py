"""LangGraph checkpoint support for resumable analysis runs.

Per-ticker SQLite databases so concurrent tickers don't contend.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from tradingagents.dataflows.utils import safe_ticker_component


class CheckpointSignatureMismatch(RuntimeError):
    """An unfinished checkpoint is bound to a different immutable context."""


def _db_path(data_dir: str | Path, ticker: str) -> Path:
    """Return the SQLite checkpoint DB path for a ticker."""
    # Reject ticker values that would escape the checkpoints directory.
    safe = safe_ticker_component(ticker).upper()
    p = Path(data_dir) / "checkpoints"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{safe}.db"


def thread_id(ticker: str, date: str, signature: str = "") -> str:
    """Deterministic thread ID for a ticker+date pair.

    ``signature`` folds in graph-shape-affecting run choices so a resume under a
    different graph can't reuse this checkpoint (#1089); omitting it keeps the
    legacy ID.
    """
    base = f"{ticker.upper()}:{date}"
    if signature:
        base = f"{base}:{signature}"
    return hashlib.sha256(base.encode()).hexdigest()[:16]


@contextmanager
def get_checkpointer(data_dir: str | Path, ticker: str) -> Generator[SqliteSaver, None, None]:
    """Context manager yielding a SqliteSaver backed by a per-ticker DB."""
    db = _db_path(data_dir, ticker)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    try:
        saver = SqliteSaver(conn)
        saver.setup()
        yield saver
    finally:
        conn.close()


def has_checkpoint(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> bool:
    """Check whether a resumable checkpoint exists for ticker+date."""
    return checkpoint_step(data_dir, ticker, date, signature) is not None


def checkpoint_step(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> int | None:
    """Return the step number of the latest checkpoint, or None if none exists."""
    db = _db_path(data_dir, ticker)
    if not db.exists():
        return None
    tid = thread_id(ticker, date, signature)
    with get_checkpointer(data_dir, ticker) as saver:
        config = {"configurable": {"thread_id": tid}}
        cp = saver.get_tuple(config)
        if cp is None:
            return None
        return cp.metadata.get("step")


def bind_checkpoint_signature(
    data_dir: str | Path, ticker: str, date: str, signature: str
) -> None:
    """Bind an unfinished run to its immutable signature.

    TradingAgents historically starts a new checkpoint thread when the graph
    shape changes. QuantLab runs need a stricter contract: an existing run for
    the same ticker/date must never be resumed or replaced under a different
    snapshot, manifest, target, or benchmark. The binding is stored beside the
    LangGraph checkpoint rows and removed only after a successful run.
    """
    db = _db_path(data_dir, ticker)
    tid = thread_id(ticker, date, signature)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ta_run_signatures (
                trade_date TEXT PRIMARY KEY,
                signature TEXT NOT NULL,
                thread_id TEXT NOT NULL
            )"""
        )
        row = conn.execute(
            "SELECT signature, thread_id FROM ta_run_signatures WHERE trade_date = ?",
            (str(date),),
        ).fetchone()
        if row is not None and row[0] != signature:
            try:
                checkpoint_exists = conn.execute(
                    "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (row[1],)
                ).fetchone()
            except sqlite3.OperationalError:
                checkpoint_exists = None
            if checkpoint_exists is not None:
                raise CheckpointSignatureMismatch(
                    "Checkpoint context mismatch for "
                    f"{ticker.upper()} on {date}; clear or finish the existing run first"
                )
            conn.execute(
                "UPDATE ta_run_signatures SET signature = ?, thread_id = ? WHERE trade_date = ?",
                (signature, tid, str(date)),
            )
        elif row is None:
            conn.execute(
                "INSERT INTO ta_run_signatures (trade_date, signature, thread_id) VALUES (?, ?, ?)",
                (str(date), signature, tid),
            )
        conn.commit()
    finally:
        conn.close()


def clear_all_checkpoints(data_dir: str | Path) -> int:
    """Remove all checkpoint DBs. Returns number of files deleted."""
    cp_dir = Path(data_dir) / "checkpoints"
    if not cp_dir.exists():
        return 0
    dbs = list(cp_dir.glob("*.db"))
    for db in dbs:
        db.unlink()
    return len(dbs)


def clear_checkpoint(data_dir: str | Path, ticker: str, date: str, signature: str = "") -> None:
    """Remove checkpoint for a specific ticker+date by deleting the thread's rows."""
    db = _db_path(data_dir, ticker)
    if not db.exists():
        return
    tid = thread_id(ticker, date, signature)
    conn = sqlite3.connect(str(db))
    try:
        for table in ("writes", "checkpoints"):
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (tid,))
        with suppress(sqlite3.OperationalError):
            conn.execute(
                "DELETE FROM ta_run_signatures WHERE trade_date = ? AND signature = ?",
                (str(date), signature),
            )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
