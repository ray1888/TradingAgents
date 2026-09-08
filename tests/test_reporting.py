"""Report parity: the shared writer produces the report tree for the CLI and the
programmatic API alike (#1037)."""

import json
from types import SimpleNamespace

import pytest

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree


def _state():
    return {
        "market_report": "MKT",
        "news_report": "NEWS",
        "investment_debate_state": {"judge_decision": "RM PLAN"},
        "trader_investment_plan": "TRADE",
        "risk_debate_state": {"judge_decision": "PM DECISION"},
    }


@pytest.mark.unit
def test_write_report_tree_creates_files(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path)
    assert out.backend == "file"
    assert out.complete_report_path.name == "complete_report.md"
    assert (tmp_path / "1_analysts" / "market.md").read_text() == "MKT"
    assert (tmp_path / "1_analysts" / "news.md").read_text() == "NEWS"
    assert (tmp_path / "2_research" / "manager.md").read_text() == "RM PLAN"
    assert (tmp_path / "3_trading" / "trader.md").read_text() == "TRADE"
    assert (tmp_path / "5_portfolio" / "decision.md").read_text() == "PM DECISION"
    complete = out.complete_report_path.read_text()
    assert "Trading Analysis Report: AAPL" in complete
    assert "MKT" in complete and "PM DECISION" in complete


@pytest.mark.unit
def test_save_reports_explicit_path(tmp_path):
    # Unbound: with an explicit save_path, the method doesn't touch self/config.
    out = TradingAgentsGraph.save_reports(None, _state(), "AAPL", save_path=tmp_path)
    assert (tmp_path / "complete_report.md").exists()
    assert out.complete_report_path == tmp_path / "complete_report.md"


@pytest.mark.unit
def test_save_reports_defaults_under_results_dir(tmp_path):
    mock_self = SimpleNamespace(config={"results_dir": str(tmp_path)})
    out = TradingAgentsGraph.save_reports(mock_self, _state(), "AAPL")
    assert out.complete_report_path.exists()
    assert out.complete_report_path.parent.parent.name == "reports"
    assert out.complete_report_path.parent.name.startswith("AAPL_")


@pytest.mark.unit
def test_provenance_appendix_is_written_without_credentials(tmp_path):
    provenance = {
        "status": "partial",
        "actual_provider": "tushare",
        "versions": {
            "snapshot_id": "snapshot-1",
            "financial_manifest_id": "manifest-1",
            "as_of_date": "2025-06-30",
        },
        "target_ticker": "600519.SH",
        "benchmark_ticker": "000300.SH",
        "requests": [{"endpoint": "/market-bars", "status": "success"}],
    }
    out = write_report_tree(_state(), "600519.SH", tmp_path, provenance=provenance)

    saved = json.loads((tmp_path / "provenance.json").read_text())
    assert saved == provenance
    assert out.backend == "file"
    assert "## VI. Data Provenance" in out.complete_report_path.read_text()
    assert "snapshot-1" in out.complete_report_path.read_text()
    assert "token" not in (tmp_path / "provenance.json").read_text().lower()


@pytest.mark.unit
def test_write_report_tree_s3_adapter_returns_presigned_zip():
    from tradingagents.report_store import S3ReportStore

    class FakeS3Client:
        def __init__(self):
            self.objects = {}

        def put_object(self, **kwargs):
            body = kwargs["Body"]
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.objects[(kwargs["Bucket"], kwargs["Key"])] = bytes(body)
            return {}

        def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
            return (
                f"https://minio.test/{Params['Bucket']}/{Params['Key']}"
                f"?expires={ExpiresIn}"
            )

    client = FakeS3Client()
    store = S3ReportStore(
        client=client,
        bucket="tradingagents",
        prefix="tradingagents/reports",
        presign_seconds=99,
    )
    out = write_report_tree(_state(), "AAPL", "AAPL_20260908", store=store)
    assert out.backend == "s3"
    assert out.download_url.endswith("report.zip?expires=99")
    assert out.complete_report_path is None
    key = "tradingagents/reports/AAPL_20260908/complete_report.md"
    assert b"Trading Analysis Report: AAPL" in client.objects[("tradingagents", key)]
    assert "secret" not in json.dumps(out.as_dict())
