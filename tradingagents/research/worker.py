"""Isolated process entry for one research analysis task."""

from __future__ import annotations

import argparse
import logging
import sys

from tradingagents.research.runner import execute_stored_run
from tradingagents.research.store import ResearchRunStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one TradingAgents research task")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = ResearchRunStore(args.db)
    try:
        execute_stored_run(args.run_id, store)
    except Exception as exc:
        logging.exception("research worker failed run_id=%s", args.run_id)
        store.update(
            args.run_id,
            status="failed",
            retryable=True,
            error=str(exc.__class__.__name__),
            clear_pid=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
