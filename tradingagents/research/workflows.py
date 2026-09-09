"""Industry thesis and daily candidate-review workflows."""

from __future__ import annotations

from tradingagents.research.news import gather_research_news
from tradingagents.research.prompts import candidate_prompt, industry_prompt
from tradingagents.research.protocol import MAX_CANDIDATES
from tradingagents.research.reports import build_report, default_provenance
from tradingagents.research.schemas import (
    CandidateReviewBody,
    CandidateReviewItem,
    IndustryThesisBody,
    QuantSignal,
    ResearchBundle,
    StructuredReport,
    VersionChange,
)
from tradingagents.research.synthesizer import ResearchSynthesizer


def _has_market_expectations(bundle: ResearchBundle) -> bool:
    materials = [
        item
        for item in bundle.market_expectation_materials
        if item.kind == "market_expectation"
    ]
    materials.extend(item for item in bundle.evidence if item.kind == "market_expectation")
    return bool(materials)


def _enforce_industry_guards(
    bundle: ResearchBundle, body: IndustryThesisBody
) -> IndustryThesisBody:
    missing = list(body.missing_materials)
    status = body.expectation_gap_status
    gap = body.expectation_gap
    if not _has_market_expectations(bundle):
        status = "pending_verification"
        if not gap:
            gap = "市场预期材料不足，预期差待验证。"
        note = "缺少可信市场预期材料，不能断言预期差成立"
        if note not in missing:
            missing.append(note)
    changes = list(body.version_changes)
    if not bundle.prior_report:
        if not changes:
            changes = [
                VersionChange(
                    judgment="首次研究，无前版可比较",
                    change="initial",
                    evidence_ids=[],
                )
            ]
        else:
            changes = [
                item.model_copy(update={"change": "initial"})
                if item.change != "initial"
                else item
                for item in changes
            ]
    allowed = {member.ticker for member in bundle.industry_members} | set(
        bundle.scope.tickers
    )
    companies = [
        company for company in body.related_companies if not allowed or company.ticker in allowed
    ]
    return body.model_copy(
        update={
            "expectation_gap_status": status,
            "expectation_gap": gap,
            "missing_materials": missing,
            "version_changes": changes,
            "related_companies": companies,
        }
    )


def run_industry_thesis(
    bundle: ResearchBundle,
    *,
    run_id: str,
    report_id: str,
    synthesizer: ResearchSynthesizer,
    model: str,
    code_version: str,
    llm_provider: str | None = None,
) -> StructuredReport:
    if bundle.research_type != "industry_thesis":
        raise ValueError("bundle research_type must be industry_thesis")
    news = gather_research_news(bundle)
    prompt = industry_prompt(bundle, news)
    body = synthesizer.generate(prompt, IndustryThesisBody)
    if not isinstance(body, IndustryThesisBody):
        body = IndustryThesisBody.model_validate(body)
    body = _enforce_industry_guards(bundle, body)
    report = build_report(
        run_id=run_id,
        report_id=report_id,
        bundle=bundle,
        news=news,
        conclusion=body.conclusion,
        missing_materials=body.missing_materials,
        applicability=body.applicability,
        provenance=default_provenance(
            model=model, code_version=code_version, llm_provider=llm_provider
        ),
        industry_thesis=body,
        supporting_evidence_ids=body.supporting_evidence_ids,
        opposing_evidence_ids=body.opposing_evidence_ids,
    )
    return report


def _copy_signal(signal: QuantSignal) -> QuantSignal:
    return QuantSignal.model_validate(signal.model_dump())


def _complete_candidate_item(
    bundle: ResearchBundle, item: CandidateReviewItem, signal: QuantSignal | None
) -> CandidateReviewItem:
    missing = list(item.missing_materials)
    industry_missing = not bundle.scope.industry_archive_present and not bundle.prior_report
    completeness = item.data_completeness
    if industry_missing:
        industry_missing = True
        if completeness is None:
            completeness = "missing_industry_context"
        note = "行业背景缺失，不自动给出负面研究结论"
        if note not in missing:
            missing.append(note)
    return item.model_copy(
        update={
            "quant_signal": _copy_signal(signal) if signal else item.quant_signal,
            "industry_background_missing": industry_missing,
            "data_completeness": completeness,
            "missing_materials": missing,
        }
    )


def run_candidate_review(
    bundle: ResearchBundle,
    *,
    run_id: str,
    report_id: str,
    synthesizer: ResearchSynthesizer,
    model: str,
    code_version: str,
    llm_provider: str | None = None,
) -> StructuredReport:
    if bundle.research_type != "candidate_review":
        raise ValueError("bundle research_type must be candidate_review")
    news = gather_research_news(bundle)
    ranked = sorted(bundle.signals, key=lambda item: item.rank)
    analyze = ranked[:MAX_CANDIDATES]
    leftover = ranked[MAX_CANDIDATES:]
    prompt = candidate_prompt(
        bundle.model_copy(update={"signals": analyze}),
        news,
    )
    body = synthesizer.generate(prompt, CandidateReviewBody)
    if not isinstance(body, CandidateReviewBody):
        body = CandidateReviewBody.model_validate(body)

    by_ticker = {
        item.ticker: item
        for item in body.items
        if item.analysis_state != "not_analyzed"
    }
    items: list[CandidateReviewItem] = []
    failed = 0
    completed = 0
    for signal in analyze:
        item = by_ticker.get(signal.ticker)
        if item is None:
            items.append(
                CandidateReviewItem(
                    ticker=signal.ticker,
                    analysis_state="failed",
                    error="model did not return this ticker",
                    quant_signal=_copy_signal(signal),
                    research_support="证据不足",
                    data_completeness="partial",
                    missing_materials=["模型未返回该标的的复核结果"],
                    industry_background_missing=not bundle.scope.industry_archive_present,
                )
            )
            failed += 1
            continue
        if item.analysis_state == "failed":
            failed += 1
            items.append(_complete_candidate_item(bundle, item, signal))
            continue
        completed += 1
        items.append(_complete_candidate_item(bundle, item, signal))
    for signal in leftover:
        items.append(
            CandidateReviewItem(
                ticker=signal.ticker,
                analysis_state="not_analyzed",
                quant_signal=_copy_signal(signal),
                rationale="超过第一期默认最多 10 只，保持未分析",
            )
        )

    status = "completed"
    if failed and completed:
        status = "partial"
    elif failed and not completed:
        status = "failed"

    merged = CandidateReviewBody(
        items=items,
        missing_materials=body.missing_materials,
        applicability=body.applicability,
    )
    conclusion = (
        f"复核 {completed} 只候选股，失败 {failed} 只，未分析 {len(leftover)} 只。"
        "研究支持度与量化信号分列，未重新估算胜率。"
    )
    report = build_report(
        run_id=run_id,
        report_id=report_id,
        bundle=bundle,
        news=news,
        conclusion=conclusion,
        missing_materials=merged.missing_materials,
        applicability=merged.applicability
        or "研究结果不改变原排名、策略参数或订单。",
        provenance=default_provenance(
            model=model, code_version=code_version, llm_provider=llm_provider
        ),
        candidate_review=merged,
        status=status,
    )
    return report
