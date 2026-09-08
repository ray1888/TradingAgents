from tests.research_fixtures import hog_bundle, load_contract
from tradingagents.research.bundle_client import parse_bundle_payload
from tradingagents.research.protocol import SCHEMA_VERSION
from tradingagents.research.schemas import RunSubmitRequest
from tradingagents.research.validation import apply_validation, validate_citations


def test_contract_submit_and_bundle_parse():
    request = RunSubmitRequest.model_validate(load_contract("submit-industry.json"))
    assert request.research_type == "industry_thesis"
    bundle = hog_bundle()
    assert bundle.schema_version == SCHEMA_VERSION
    assert bundle.bundle_id == request.bundle_id


def test_wrapped_quantlab_payload_unwraps():
    raw = load_contract("bundle-industry-hog.json")
    parsed = parse_bundle_payload(
        {
            "data": raw,
            "meta": {
                "request_id": "r1",
                "provider": "quantlab",
                "build_revision": "ql-1",
                "versions": {},
                "data_range": {},
            },
        }
    )
    assert parsed.bundle_id == raw["bundle_id"]


def test_unknown_schema_is_rejected():
    payload = load_contract("submit-industry.json")
    payload["schema_version"] = "99"
    try:
        RunSubmitRequest.model_validate(payload)
    except Exception as exc:
        assert "schema_version" in str(exc)
    else:
        raise AssertionError("expected schema rejection")


def test_unknown_citation_fails_validation():
    from tradingagents.research.news import analyze_bundle_news
    from tradingagents.research.reports import build_report, default_provenance
    from tradingagents.research.schemas import IndustryThesisBody

    bundle = hog_bundle()
    report = build_report(
        run_id="run_x",
        report_id="rpt_x",
        bundle=bundle,
        news=analyze_bundle_news(bundle),
        conclusion="x",
        missing_materials=[],
        applicability="x",
        provenance=default_provenance(model="stub", code_version="test"),
        industry_thesis=IndustryThesisBody(
            conclusion="x",
            expectation_gap_status="pending_verification",
            applicability="x",
            supporting_evidence_ids=["does-not-exist"],
        ),
        supporting_evidence_ids=["does-not-exist"],
    )
    assert report.status == "incomplete"
    assert validate_citations(report, bundle.evidence_ids())
    assert apply_validation(report, bundle.evidence_ids()).validation_errors
