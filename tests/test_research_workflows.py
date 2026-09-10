from copy import deepcopy

from tests.research_fixtures import (
    candidate_bundle,
    candidate_synthesizer,
    hog_bundle,
    hog_synthesizer,
)
from tradingagents.research.prompts import INDUSTRY_RULES, candidate_prompt, industry_prompt
from tradingagents.research.runner import isolated_run_config
from tradingagents.research.workflows import run_candidate_review, run_industry_thesis


def test_industry_thesis_pending_gap_without_market_expectations():
    report = run_industry_thesis(
        hog_bundle(),
        run_id="run_hog",
        report_id="rpt_hog",
        synthesizer=hog_synthesizer(),
        model="stub-model",
        code_version="test",
        llm_provider="stub",
    )
    assert report.industry_thesis is not None
    assert report.industry_thesis.expectation_gap_status == "pending_verification"
    assert any("市场预期" in item for item in report.missing_materials)
    assert report.industry_thesis.version_changes[0].change == "initial"
    assert report.readable_markdown
    assert "ev-ind-1" in report.supporting_evidence_ids


def test_industry_prompt_requires_separated_claims_and_falsifiers():
    from tradingagents.research.news import analyze_bundle_news

    prompt = industry_prompt(hog_bundle(), analyze_bundle_news(hog_bundle())).lower()
    rules = INDUSTRY_RULES.lower()
    blob = rules + prompt
    for needle in (
        "supply",
        "market expectations",
        "falsif",
        "pending_verification",
        "valuation references are not live market consensus",
    ):
        assert needle.lower() in blob


def test_prior_report_can_weaken_not_only_strengthen():
    from tradingagents.research.synthesizer import CallableSynthesizer

    bundle = hog_bundle().model_copy(
        update={
            "prior_report": {"report_id": "old", "conclusion": "去化很快"},
            "versions": hog_bundle().versions.model_copy(update={"prior_report_id": "old"}),
        }
    )

    def _fn(prompt, schema):
        payload = hog_synthesizer()._fn(prompt, schema)
        payload["version_changes"] = [
            {
                "judgment": "新的存栏数据显示去化慢于前版",
                "change": "weakened",
                "evidence_ids": ["ev-ind-1"],
            }
        ]
        payload["expectation_gap_status"] = "pending_verification"
        return payload

    report = run_industry_thesis(
        bundle,
        run_id="run_hog2",
        report_id="rpt_hog2",
        synthesizer=CallableSynthesizer(_fn),
        model="stub",
        code_version="test",
    )
    assert report.industry_thesis.version_changes[0].change == "weakened"


def test_candidate_review_keeps_quant_metrics_and_support_labels():
    bundle = candidate_bundle()
    report = run_candidate_review(
        bundle,
        run_id="run_c",
        report_id="rpt_c",
        synthesizer=candidate_synthesizer(),
        model="stub",
        code_version="test",
    )
    by_ticker = {item.ticker: item for item in report.candidate_review.items}
    assert by_ticker["002714.SZ"].research_support == "支持"
    assert by_ticker["600519.SS"].research_support == "反对"
    assert by_ticker["000858.SZ"].research_support == "证据不足"
    assert by_ticker["002714.SZ"].quant_signal.quant_metrics["win_rate"] == 0.54
    assert by_ticker["002714.SZ"].quant_signal.rank == 1
    assert by_ticker["600519.SS"].industry_background_missing is True
    prompt = candidate_prompt(bundle, report.news)
    assert "do not re-estimate win rate" in prompt.lower() or "do not recompute" in prompt.lower()


def test_mixed_research_support_is_allowed():
    from tradingagents.research.synthesizer import CallableSynthesizer

    def _fn(prompt, schema):
        payload = candidate_synthesizer()._fn(prompt, schema)
        payload["items"][0]["research_support"] = "混合"
        payload["items"][0]["rationale"] = "部分证据支持、部分冲突"
        return payload

    report = run_candidate_review(
        candidate_bundle(),
        run_id="run_m",
        report_id="rpt_m",
        synthesizer=CallableSynthesizer(_fn),
        model="stub",
        code_version="test",
    )
    assert report.candidate_review.items[0].research_support == "混合"


def test_candidate_partial_failure():
    report = run_candidate_review(
        candidate_bundle(),
        run_id="run_p",
        report_id="rpt_p",
        synthesizer=candidate_synthesizer(fail_ticker="000858.SZ"),
        model="stub",
        code_version="test",
    )
    assert report.status == "partial"
    states = {item.ticker: item.analysis_state for item in report.candidate_review.items}
    assert states["000858.SZ"] == "failed"
    assert states["002714.SZ"] == "completed"


def test_extra_candidates_are_not_analyzed():
    bundle = candidate_bundle()
    extra = []
    for index in range(4, 13):
        extra.append(
            bundle.signals[0].model_copy(
                update={"ticker": f"00000{index}.SZ", "rank": index, "quant_metrics": {"rank_score": 0.1}}
            )
        )
    bundle = bundle.model_copy(update={"signals": [*bundle.signals, *extra]})
    report = run_candidate_review(
        bundle,
        run_id="run_n",
        report_id="rpt_n",
        synthesizer=candidate_synthesizer(),
        model="stub",
        code_version="test",
    )
    not_analyzed = [item for item in report.candidate_review.items if item.analysis_state == "not_analyzed"]
    assert len(not_analyzed) == 2


def test_isolated_config_uses_bundle_versions_not_latest():
    first = isolated_run_config(hog_bundle())
    other = candidate_bundle()
    second = isolated_run_config(other, first)
    assert second["snapshot_id"] == "snap-cand-1"
    assert second["financial_manifest_id"] == "fin-cand-1"
    assert second["as_of_date"] == other.as_of_date
    assert first["snapshot_id"] == "snap-hog-1"
    mutated = deepcopy(first)
    mutated["snapshot_id"] = "leaked"
    third = isolated_run_config(hog_bundle())
    assert third["snapshot_id"] == "snap-hog-1"
