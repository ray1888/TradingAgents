"""Adapt QuantLab frozen bundles and report wire format."""

from __future__ import annotations

from typing import Any

from tradingagents.research.protocol import PROMPT_VERSION
from tradingagents.research.schemas import (
    BundleScope,
    BundleVersions,
    CandidateReviewItem,
    CompanyProfile,
    EvidenceItem,
    IndustryMember,
    QuantSignal,
    ResearchBundle,
    StructuredReport,
)

_KIND = {
    "indicator": "industry_indicator",
    "announcement": "official_disclosure",
    "news": "media",
    "valuation_opinion": "valuation_reference",
    "official_disclosure": "official_disclosure",
    "media": "media",
    "commentary": "commentary",
    "rumor": "rumor",
    "industry_indicator": "industry_indicator",
    "company_profile": "company_profile",
    "signal": "signal",
    "market_expectation": "market_expectation",
    "valuation_reference": "valuation_reference",
    "prior_report": "prior_report",
}

_SOURCE = {
    "mara": "moa",
    "nbs": "nbs",
    "cninfo": "cninfo",
    "tushare_cls": "cls",
    "tushare_yicai": "yicai",
}


def _string_versions(raw: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if value is None or value == "":
            continue
        out[str(key)] = str(value)
    return out


def _evidence_item(raw: dict[str, Any]) -> EvidenceItem:
    kind = _KIND.get(str(raw.get("kind") or "media"), "media")
    source = str(raw.get("source") or raw.get("source_identity") or "unknown")
    return EvidenceItem(
        evidence_id=str(raw.get("evidence_id") or raw.get("id") or source),
        kind=kind,
        source_identity=_SOURCE.get(source, source),
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or raw.get("content") or ""),
        published_at=raw.get("published_at"),
        first_seen_at=raw.get("first_seen_at"),
        available_at=raw.get("available_at"),
        content_hash=raw.get("content_hash"),
        url=raw.get("url") or raw.get("source_url"),
        vendor_record_id=raw.get("vendor_record_id") or raw.get("provider_id"),
        tickers=list(raw.get("tickers") or []),
        is_repost=bool(raw.get("is_repost")),
        canonical_evidence_id=raw.get("canonical_evidence_id"),
        coverage_gap=raw.get("coverage_gap"),
    )


def normalize_bundle(payload: dict[str, Any]) -> ResearchBundle:
    """Accept either this repo's contract bundle or QuantLab's frozen payload."""
    if isinstance(payload.get("versions"), dict) and (
        "industry_members" in payload or "signals" in payload
    ):
        bundle = ResearchBundle.model_validate(payload)
        if not bundle.wire_versions:
            bundle = bundle.model_copy(
                update={"wire_versions": _string_versions(payload.get("versions"))}
            )
        return bundle

    versions_raw = payload.get("versions") or {}
    snapshot_id = str(versions_raw.get("snapshot_id") or payload.get("snapshot_id") or "")
    financial_id = str(
        versions_raw.get("financial_manifest_id") or payload.get("financial_manifest_id") or ""
    )
    if not snapshot_id or not financial_id:
        raise ValueError("frozen bundle is missing snapshot or financial version")
    tickers = list(payload.get("tickers") or [])
    members = [
        IndustryMember(ticker=item["ticker"], name=item.get("l3_name") or item.get("l1_name"))
        for item in payload.get("industry_membership") or []
        if item.get("ticker")
    ]
    if not members:
        members = [IndustryMember(ticker=ticker) for ticker in tickers]
    signals = []
    for item in payload.get("candidates") or payload.get("signals") or []:
        reasons = item.get("signal_reasons")
        if reasons is None:
            reason = item.get("reason")
            reasons = [str(reason)] if reason else []
        signals.append(
            QuantSignal(
                ticker=str(item["ticker"]),
                rank=int(item.get("rank") or item.get("original_rank") or 0),
                strategy_name=str(item.get("strategy_name") or ""),
                signal_reasons=[str(part) for part in reasons],
                holding_horizon=str(item.get("holding_horizon") or "") or None,
                selection_run_id=payload.get("selection_run_id"),
                quant_metrics={},
            )
        )
    coverage = payload.get("coverage") or {}
    notes = []
    for source, info in coverage.items():
        if isinstance(info, dict) and info.get("status") not in {None, "available"}:
            notes.append(f"{source}: {info.get('status')}")
    evidence = [_evidence_item(item) for item in payload.get("evidence") or []]
    prior = payload.get("previous_report") or payload.get("prior_report")
    return ResearchBundle(
        schema_version=str(payload.get("schema_version") or "1"),
        bundle_id=str(payload.get("bundle_id") or ""),
        research_type=payload["research_type"],
        as_of_time=str(payload["as_of_time"]),
        as_of_date=str(payload.get("as_of_date") or payload["as_of_time"][:10]),
        versions=BundleVersions(
            snapshot_id=snapshot_id,
            financial_manifest_id=financial_id,
            industry_members_version=str(
                versions_raw.get("industry_manifest_id")
                or versions_raw.get("industry_members_version")
                or ""
            )
            or None,
            evidence_version=str(versions_raw.get("evidence_version") or "") or None,
            prior_report_id=payload.get("previous_report_id") or versions_raw.get("prior_report_id"),
        ),
        wire_versions=_string_versions(versions_raw),
        scope=BundleScope(
            theme=payload.get("topic") or payload.get("scope", {}).get("theme"),
            tickers=tickers,
            horizon=payload.get("horizon") or payload.get("scope", {}).get("horizon"),
            coverage=coverage if isinstance(coverage, dict) else {},
            industry_archive_present=bool(prior or payload.get("industry_membership")),
        ),
        industry_members=members,
        company_profiles=[
            CompanyProfile(ticker=ticker) for ticker in tickers
        ],
        evidence=evidence,
        signals=signals,
        prior_report=prior if isinstance(prior, dict) else None,
        coverage_notes=notes,
    )


def _claims(items) -> list[dict[str, Any]]:
    claims = []
    for item in items or []:
        claims.append({"text": item.statement, "evidence_ids": list(item.evidence_ids)})
    return claims


def _candidate(item: CandidateReviewItem) -> dict[str, Any]:
    status = item.analysis_state if item.analysis_state in {"completed", "failed"} else "completed"
    if item.analysis_state == "not_analyzed":
        status = "completed"
        support = "证据不足"
        error = None
        conclusion = item.rationale or "未分析"
    else:
        support = item.research_support or "证据不足"
        error = item.error
        conclusion = item.rationale or item.applicability or ""
    return {
        "ticker": item.ticker,
        "status": status,
        "support": support,
        "coverage": item.data_completeness or ("行业背景缺失" if item.industry_background_missing else ""),
        "conclusion": conclusion,
        "risks": [risk.statement for risk in item.risks] or ([item.error] if item.error else []),
        "horizon": (item.quant_signal.holding_horizon if item.quant_signal else None)
        or item.cycle_match_rationale
        or "",
        "evidence_ids": list(item.evidence_ids),
        "error": error,
    }


def to_quantlab_report(report: StructuredReport) -> dict[str, Any]:
    """Shape QuantLab validates with extra=forbid ResearchReport."""
    thesis = report.industry_thesis
    review = report.candidate_review
    supporting = _claims(thesis.facts if thesis else []) or [
        {"text": report.conclusion, "evidence_ids": list(report.supporting_evidence_ids)}
    ]
    if not supporting[0]["evidence_ids"] and report.supporting_evidence_ids:
        supporting[0]["evidence_ids"] = list(report.supporting_evidence_ids)
    opposing = _claims(thesis.falsifiers if thesis else [])
    if report.opposing_evidence_ids and not opposing:
        opposing = [{"text": "反证", "evidence_ids": list(report.opposing_evidence_ids)}]
    if thesis:
        hypotheses = [item.statement for item in thesis.hypotheses] or [thesis.conclusion]
        expectations = [item.statement for item in thesis.market_expectations] or [
            thesis.expectation_gap or "预期差待验证"
        ]
        catalysts = [item.statement for item in thesis.catalysts] or hypotheses
        tracking = list(thesis.tracking_indicators) or ["待补充跟踪指标"]
        falsifiers = [item.statement for item in thesis.falsifiers] or ["待补充证伪条件"]
        changes = [f"{item.change}: {item.judgment}" for item in thesis.version_changes]
    else:
        hypotheses = expectations = catalysts = tracking = falsifiers = changes = []
    cited = list(dict.fromkeys(report.supporting_evidence_ids + report.opposing_evidence_ids))
    if review:
        for item in review.items:
            cited.extend(item.evidence_ids)
        candidates = [_candidate(item) for item in review.items if item.analysis_state != "not_analyzed"]
    else:
        candidates = []
    versions = dict(report.input_snapshot.get("wire_versions") or {})
    if not versions:
        versions = {
            key: str(value)
            for key, value in report.versions.model_dump().items()
            if value not in (None, "")
        }
    supporting = [item for item in supporting if item["evidence_ids"]]
    opposing = [item for item in opposing if item["evidence_ids"]]
    return {
        "schema_version": "1",
        "report_id": report.report_id,
        "run_id": report.run_id,
        "research_type": report.research_type,
        "bundle_id": report.bundle_id,
        "versions": versions,
        "conclusion": report.conclusion,
        "supporting": [item for item in supporting if item["evidence_ids"] or item["text"]],
        "opposing": opposing,
        "missing": list(report.missing_materials),
        "evidence_ids": list(dict.fromkeys(cited)),
        "model_version": report.provenance.model,
        "prompt_version": report.provenance.prompt_version or PROMPT_VERSION,
        "code_version": report.provenance.code_version,
        "generated_at": report.provenance.generated_at,
        "hypotheses": hypotheses,
        "expectations": expectations,
        "catalysts": catalysts,
        "tracking_points": tracking,
        "falsification": falsifiers,
        "changes": changes,
        "candidates": candidates,
        "readable": report.readable_markdown,
        "status": report.status,
        "coverage_gaps": list(report.news.coverage_gaps),
        "used_news_evidence": [
            item.model_dump(mode="json") for item in report.news.used_news_evidence
        ],
        "mcp_status": report.news.mcp_status,
    }
