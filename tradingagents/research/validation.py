"""Citation and report completeness checks against a frozen bundle."""

from __future__ import annotations

from tradingagents.research.schemas import CitedClaim, StructuredReport


def _collect_claim_ids(claims: list[CitedClaim]) -> list[str]:
    ids: list[str] = []
    for claim in claims:
        ids.extend(claim.evidence_ids)
    return ids


def collect_cited_ids(report: StructuredReport) -> list[str]:
    ids: list[str] = []
    ids.extend(report.supporting_evidence_ids)
    ids.extend(report.opposing_evidence_ids)
    for event in report.news.events:
        ids.append(event.evidence_id)
        if event.canonical_evidence_id:
            ids.append(event.canonical_evidence_id)
    thesis = report.industry_thesis
    if thesis is not None:
        for group in (
            thesis.hypotheses,
            thesis.facts,
            thesis.market_expectations,
            thesis.inferences,
            thesis.catalysts,
            thesis.falsifiers,
        ):
            ids.extend(_collect_claim_ids(group))
        ids.extend(thesis.supporting_evidence_ids)
        ids.extend(thesis.opposing_evidence_ids)
        for company in thesis.related_companies:
            ids.extend(company.evidence_ids)
        for change in thesis.version_changes:
            ids.extend(change.evidence_ids)
    review = report.candidate_review
    if review is not None:
        for item in review.items:
            ids.extend(item.evidence_ids)
            for event in item.event_impacts:
                ids.append(event.evidence_id)
            ids.extend(_collect_claim_ids(item.risks))
    return ids


def validate_citations(report: StructuredReport, known_ids: set[str]) -> list[str]:
    errors: list[str] = []
    for evidence_id in collect_cited_ids(report):
        if evidence_id not in known_ids:
            errors.append(f"unknown evidence_id {evidence_id!r}")
    return errors


def apply_validation(report: StructuredReport, known_ids: set[str]) -> StructuredReport:
    errors = list(dict.fromkeys([*report.validation_errors, *validate_citations(report, known_ids)]))
    if report.research_type == "industry_thesis" and report.industry_thesis is None:
        errors.append("industry_thesis body is required")
    if report.research_type == "candidate_review" and report.candidate_review is None:
        errors.append("candidate_review body is required")
    status = report.status
    if errors and status in ("completed", "partial"):
        status = "incomplete"
    return report.model_copy(update={"validation_errors": errors, "status": status})
