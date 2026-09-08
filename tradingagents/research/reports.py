"""JSON + Markdown research reports."""

from __future__ import annotations

from datetime import datetime, timezone

from tradingagents.research.protocol import SCHEMA_VERSION
from tradingagents.research.schemas import (
    CandidateReviewBody,
    IndustryThesisBody,
    NewsAnalysis,
    ReportProvenance,
    ResearchBundle,
    StructuredReport,
)
from tradingagents.research.validation import apply_validation


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def input_snapshot(bundle: ResearchBundle) -> dict:
    return {
        "bundle_id": bundle.bundle_id,
        "research_type": bundle.research_type,
        "as_of_time": bundle.as_of_time,
        "as_of_date": bundle.as_of_date,
        "versions": bundle.versions.model_dump(),
        "theme": bundle.scope.theme,
        "tickers": bundle.scope.tickers
        or [item.ticker for item in bundle.industry_members]
        or [item.ticker for item in bundle.signals],
        "industry_members_version": bundle.versions.industry_members_version,
        "evidence_version": bundle.versions.evidence_version,
        "evidence_count": len(bundle.evidence),
        "signal_count": len(bundle.signals),
        "prior_report_id": bundle.versions.prior_report_id,
    }


def render_markdown(report: StructuredReport) -> str:
    lines = [
        f"# Research Report ({report.research_type})",
        "",
        f"- Report ID: {report.report_id}",
        f"- Run ID: {report.run_id}",
        f"- Bundle: {report.bundle_id}",
        f"- As of: {report.as_of_time}",
        f"- Status: {report.status}",
        "",
        "## Conclusion",
        "",
        report.conclusion,
        "",
        "## Missing materials",
        "",
    ]
    if report.missing_materials:
        lines.extend(f"- {item}" for item in report.missing_materials)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Applicability", "", report.applicability, ""])
    if report.industry_thesis:
        thesis = report.industry_thesis
        lines.extend(
            [
                "## Industry thesis",
                "",
                f"Expectation gap: {thesis.expectation_gap_status}",
                "",
                thesis.expectation_gap or "",
                "",
                "### Catalysts",
            ]
        )
        for item in thesis.catalysts:
            lines.append(f"- {item.statement} ({', '.join(item.evidence_ids)})")
        lines.extend(["", "### Falsifiers"])
        for item in thesis.falsifiers:
            lines.append(f"- {item.statement} ({', '.join(item.evidence_ids)})")
        lines.extend(["", "### Version changes"])
        for item in thesis.version_changes:
            lines.append(f"- {item.change}: {item.judgment}")
    if report.candidate_review:
        lines.extend(["## Candidate review", ""])
        for item in report.candidate_review.items:
            rank = item.quant_signal.rank if item.quant_signal else "-"
            lines.append(
                f"- {item.ticker} rank={rank} state={item.analysis_state} "
                f"support={item.research_support} completeness={item.data_completeness}"
            )
            if item.rationale:
                lines.append(f"  {item.rationale}")
    if report.validation_errors:
        lines.extend(["", "## Validation errors"])
        lines.extend(f"- {error}" for error in report.validation_errors)
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            f"- Model: {report.provenance.model}",
            f"- Prompt: {report.provenance.prompt_version}",
            f"- Code: {report.provenance.code_version}",
            f"- Generated: {report.provenance.generated_at}",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def build_report(
    *,
    run_id: str,
    report_id: str,
    bundle: ResearchBundle,
    news: NewsAnalysis,
    conclusion: str,
    missing_materials: list[str],
    applicability: str,
    provenance: ReportProvenance,
    industry_thesis: IndustryThesisBody | None = None,
    candidate_review: CandidateReviewBody | None = None,
    supporting_evidence_ids: list[str] | None = None,
    opposing_evidence_ids: list[str] | None = None,
    status: str = "completed",
) -> StructuredReport:
    report = StructuredReport(
        schema_version=SCHEMA_VERSION,
        report_id=report_id,
        run_id=run_id,
        research_type=bundle.research_type,
        bundle_id=bundle.bundle_id,
        as_of_time=bundle.as_of_time,
        as_of_date=bundle.as_of_date,
        versions=bundle.versions,
        input_snapshot=input_snapshot(bundle),
        news=news,
        industry_thesis=industry_thesis,
        candidate_review=candidate_review,
        conclusion=conclusion,
        supporting_evidence_ids=supporting_evidence_ids or [],
        opposing_evidence_ids=opposing_evidence_ids or [],
        missing_materials=missing_materials,
        applicability=applicability,
        status=status,  # type: ignore[arg-type]
        provenance=provenance,
    )
    report = apply_validation(report, bundle.evidence_ids())
    report = report.model_copy(update={"readable_markdown": render_markdown(report)})
    return report


def default_provenance(*, model: str, code_version: str, llm_provider: str | None = None) -> ReportProvenance:
    from tradingagents.research.protocol import PROMPT_VERSION

    return ReportProvenance(
        model=model,
        prompt_version=PROMPT_VERSION,
        code_version=code_version,
        generated_at=_now(),
        llm_provider=llm_provider,
    )
