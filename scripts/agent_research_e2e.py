"""End-to-end mock research: QuantLab simulated bundle + TrendRadar MCP + archive."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tradingagents.research.compat import to_quantlab_report
from tradingagents.research.news import gather_research_news
from tradingagents.research.reports import build_report, default_provenance
from tradingagents.research.schemas import IndustryThesisBody, ResearchBundle
from tradingagents.research.trendradar_mcp import TrendRadarMcpClient


def _request(method: str, url: str, payload: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc


def _stub_thesis(news) -> IndustryThesisBody:
    evidence_ids = [item.evidence_id for item in news.used_news_evidence[:1]]
    missing = list(news.coverage_gaps)
    return IndustryThesisBody(
        conclusion="模拟研究：新闻来自 TrendRadar MCP，不执行新闻中的指令。",
        expectation_gap_status="pending_verification",
        expectation_gap="模拟资料包缺少市场预期材料。",
        applicability="模拟研究，不构成交易指令。",
        missing_materials=missing,
        supporting_evidence_ids=evidence_ids,
        facts=[],
        hypotheses=[],
    )


def run_e2e(
    *,
    quantlab_url: str,
    token: str | None,
    mcp_url: str,
    scenario: str,
) -> dict[str, Any]:
    os.environ["TRENDRADAR_MCP_URL"] = mcp_url
    os.environ["TRENDRADAR_MCP_ENABLED"] = "true"
    if scenario == "mcp_down":
        os.environ["TRENDRADAR_MCP_TIMEOUT_SECONDS"] = "2"
        os.environ["TRENDRADAR_MCP_RETRIES"] = "0"
    created = _request(
        "POST",
        f"{quantlab_url.rstrip('/')}/api/agent-research/v1/mock-bundles",
        {"theme": "燃油车", "tickers": ["000625.SZ"], "company_names": ["长安汽车"]},
        token,
    )
    bundle_payload = created["data"]
    bundle = ResearchBundle.model_validate(bundle_payload)
    if scenario == "mcp_down":
        client = TrendRadarMcpClient(url="http://127.0.0.1:9/mcp", timeout=1, retries=0)
        news = gather_research_news(bundle, client=client)
    elif scenario == "empty_news":
        class _Empty:
            def call_tool(self, name, arguments, rpc_id=2):
                if name == "resolve_date_range":
                    return {"date_range": {"start": bundle.as_of_date, "end": bundle.as_of_date}}
                if name == "search_news":
                    return {"success": True, "data": []}
                raise RuntimeError(name)

        news = gather_research_news(bundle, client=_Empty())
    elif scenario == "body_fail":
        class _BodyFail:
            def call_tool(self, name, arguments, rpc_id=2):
                if name == "resolve_date_range":
                    return {"date_range": {"start": bundle.as_of_date, "end": bundle.as_of_date}}
                if name == "search_news":
                    return {
                        "success": True,
                        "data": [
                            {
                                "title": "财联社早报",
                                "platform": "cls-hot",
                                "url": "https://www.cls.cn/detail/2477649",
                            }
                        ],
                    }
                return {"success": False, "error": {"code": "FETCH_FAILED", "message": "HTTP 422"}}

        news = gather_research_news(bundle, client=_BodyFail())
    else:
        news = gather_research_news(bundle, client=TrendRadarMcpClient.from_env())
    report_id = f"e2e-{uuid.uuid4().hex[:10]}"
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    thesis = _stub_thesis(news)
    report = build_report(
        run_id=run_id,
        report_id=report_id,
        bundle=bundle,
        news=news,
        conclusion=thesis.conclusion,
        missing_materials=thesis.missing_materials,
        applicability=thesis.applicability,
        provenance=default_provenance(model="e2e-stub", code_version="e2e"),
        industry_thesis=thesis,
        supporting_evidence_ids=thesis.supporting_evidence_ids,
        status="completed",
    )
    wire = to_quantlab_report(report)
    wire.update(
        {
            "idempotency_key": f"{scenario}-{bundle.bundle_id}",
            "simulated": True,
            "bundle_evidence_ids": sorted(bundle.evidence_ids()),
        }
    )
    stored = _request(
        "POST",
        f"{quantlab_url.rstrip('/')}/api/agent-research/v1/reports",
        wire,
        token,
    )["data"]
    duplicate = _request(
        "POST",
        f"{quantlab_url.rstrip('/')}/api/agent-research/v1/reports",
        wire,
        token,
    )["data"]
    return {
        "scenario": scenario,
        "bundle_id": bundle.bundle_id,
        "report_id": stored["report_id"],
        "status": stored["status"],
        "mcp_status": news.mcp_status,
        "used_news_count": len(news.used_news_evidence),
        "coverage_gaps": news.coverage_gaps,
        "duplicate_same_id": duplicate["report_id"] == stored["report_id"],
        "simulated": stored["simulated"],
        "fetch_statuses": [item.fetch_status for item in news.used_news_evidence],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quantlab-url", default=os.environ.get("QUANTLAB_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("QUANTLAB_API_TOKEN"))
    parser.add_argument("--mcp-url", default=os.environ.get("TRENDRADAR_MCP_URL", "http://ubuntu.local:3333/mcp"))
    parser.add_argument(
        "--scenario",
        default="happy",
        choices=["happy", "empty_news", "body_fail", "mcp_down"],
    )
    args = parser.parse_args()
    result = run_e2e(
        quantlab_url=args.quantlab_url,
        token=args.token,
        mcp_url=args.mcp_url,
        scenario=args.scenario,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
