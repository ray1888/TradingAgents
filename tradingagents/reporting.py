"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus a consolidated ``complete_report.md``. A report-store adapter
then persists that tree to the local filesystem or S3/MinIO, selected by
``report_store`` in config.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from tradingagents.report_store import (
    ReportStoreAdapter,
    ReportStoreResult,
    build_report_store,
)


def _section_files(final_state: dict) -> tuple[dict[str, str], list[str]]:
    files: dict[str, str] = {}
    sections: list[str] = []

    analyst_parts = []
    if final_state.get("market_report"):
        files["1_analysts/market.md"] = final_state["market_report"]
        analyst_parts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        files["1_analysts/sentiment.md"] = final_state["sentiment_report"]
        analyst_parts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        files["1_analysts/news.md"] = final_state["news_report"]
        analyst_parts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        files["1_analysts/fundamentals.md"] = final_state["fundamentals_report"]
        analyst_parts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{content}")

    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research_parts = []
        if debate.get("bull_history"):
            files["2_research/bull.md"] = debate["bull_history"]
            research_parts.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            files["2_research/bear.md"] = debate["bear_history"]
            research_parts.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            files["2_research/manager.md"] = debate["judge_decision"]
            research_parts.append(("Research Manager", debate["judge_decision"]))
        if research_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in research_parts)
            sections.append(f"## II. Research Team Decision\n\n{content}")

    if final_state.get("trader_investment_plan"):
        files["3_trading/trader.md"] = final_state["trader_investment_plan"]
        sections.append(
            "## III. Trading Team Plan\n\n### Trader\n"
            + final_state["trader_investment_plan"]
        )

    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_parts = []
        if risk.get("aggressive_history"):
            files["4_risk/aggressive.md"] = risk["aggressive_history"]
            risk_parts.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            files["4_risk/conservative.md"] = risk["conservative_history"]
            risk_parts.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            files["4_risk/neutral.md"] = risk["neutral_history"]
            risk_parts.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in risk_parts)
            sections.append(f"## IV. Risk Management Team Decision\n\n{content}")
        if risk.get("judge_decision"):
            files["5_portfolio/decision.md"] = risk["judge_decision"]
            sections.append(
                "## V. Portfolio Manager Decision\n\n### Portfolio Manager\n"
                + risk["judge_decision"]
            )

    return files, sections


def build_report_files(
    final_state: dict,
    ticker: str,
    provenance: dict | None = None,
) -> dict[str, str]:
    """Return the relative markdown/json tree for one completed run."""
    files, sections = _section_files(final_state)
    if provenance is not None:
        files["provenance.json"] = json.dumps(
            provenance, ensure_ascii=False, indent=2, default=str
        )
        versions = provenance.get("versions", {})
        provenance_lines = [
            "## VI. Data Provenance",
            "",
            f"- Status: {provenance.get('status', 'unknown')}",
            f"- Provider: {provenance.get('actual_provider', 'unknown')}",
            f"- Snapshot: {versions.get('snapshot_id', 'unknown')}",
            f"- Financial manifest: {versions.get('financial_manifest_id', 'unknown')}",
            f"- As-of date: {versions.get('as_of_date', 'unknown')}",
            f"- Target: {provenance.get('target_ticker', ticker)}",
            f"- Benchmark: {provenance.get('benchmark_ticker', 'unknown')}",
            "",
            "See `provenance.json` for request-level audit details.",
        ]
        sections.append("\n".join(provenance_lines))
    header = (
        f"# Trading Analysis Report: {ticker}\n\n"
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )
    files["complete_report.md"] = header + "\n\n".join(sections)
    return files


def _store_destination(store: ReportStoreAdapter, save_path: Path) -> str | Path:
    if store.backend == "s3":
        return Path(save_path).name
    return save_path


def write_report_tree(
    final_state: dict,
    ticker: str,
    save_path,
    provenance: dict | None = None,
    *,
    config: Mapping | None = None,
    store: ReportStoreAdapter | None = None,
) -> ReportStoreResult:
    """Persist a completed run's reports via the configured store adapter."""
    save_path = Path(save_path)
    adapter = store or build_report_store(config)
    files = build_report_files(final_state, ticker, provenance)
    return adapter.save(files, destination=_store_destination(adapter, save_path))
