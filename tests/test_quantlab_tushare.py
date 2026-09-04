from __future__ import annotations

import json
from copy import deepcopy

import pytest

import tradingagents.dataflows.quantlab_tushare as quantlab
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.dataflows.quantlab_tushare import (
    QuantLabError,
    get_quantlab_fundamentals,
    prepare_quantlab_run,
    provenance_for_config,
    write_failed_provenance,
)


def _base_config():
    return {
        "research_profile": "quantlab_a_share",
        "quantlab_base_url": "http://quantlab.test",
        "quantlab_api_token": "do-not-leak-this-token",
        "snapshot_id": "snapshot-1",
        "financial_manifest_id": "manifest-1",
        "as_of_date": "2025-06-30",
        "benchmark_ticker": "000300.SH",
        "quantlab_target_ticker": "600519",
        "results_dir": "./results",
        "data_vendors": {"news_data": "yfinance"},
        "tool_vendors": {},
    }


def _meta(*, price_basis="hfq"):
    return {
        "request_id": "request-1",
        "provider": "tushare",
        "price_basis": price_basis,
        "build_revision": "quantlab-rev",
        "versions": {
            "snapshot_id": "snapshot-1",
            "financial_manifest_id": "manifest-1",
            "as_of_date": "2025-06-30",
        },
        "data_range": {"start_date": "2020-01-01", "end_date": "2025-06-30"},
    }


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def request(self, method, path, **kwargs):
        assert "do-not-leak-this-token" not in repr(kwargs)
        return deepcopy(self.responses.pop(0))


def _resolved_config(monkeypatch):
    response = {
        "data": {
            "ticker": "600519.SH",
            "benchmark_ticker": "000300.SH",
            "snapshot_id": "snapshot-1",
            "financial_manifest_id": "manifest-1",
            "as_of_date": "2025-06-30",
        },
        "meta": _meta(),
    }
    monkeypatch.setattr(quantlab, "_client", lambda config: _FakeClient([response]))
    return prepare_quantlab_run(_base_config())


@pytest.mark.unit
def test_preflight_binds_exact_versions_and_strict_vendors(monkeypatch):
    prepared = _resolved_config(monkeypatch)

    assert prepared["quantlab_target_ticker"] == "600519.SH"
    assert prepared["_quantlab_context"]["snapshot_id"] == "snapshot-1"
    assert prepared["_quantlab_context"]["financial_manifest_id"] == "manifest-1"
    for category in ("core_stock_apis", "technical_indicators", "fundamental_data"):
        assert prepared["data_vendors"][category] == "quantlab_tushare"
    assert prepared["data_vendors"]["news_data"] == "yfinance"


@pytest.mark.unit
def test_quantlab_vendor_never_falls_back_to_yfinance(monkeypatch):
    old = get_config()
    calls = []
    try:
        config = _base_config()
        config["data_vendors"]["core_stock_apis"] = "quantlab_tushare"
        set_config(config, replace=True)

        def fail_quantlab(*args, **kwargs):
            raise QuantLabError("AUTH_FAILED", "denied")

        def track_yfinance(*args, **kwargs):
            calls.append("yfinance")
            return "unexpected"

        monkeypatch.setitem(
            interface.VENDOR_METHODS["get_stock_data"], "quantlab_tushare", fail_quantlab
        )
        monkeypatch.setitem(interface.VENDOR_METHODS["get_stock_data"], "yfinance", track_yfinance)

        with pytest.raises(QuantLabError, match="AUTH_FAILED"):
            interface.route_to_vendor("get_stock_data", "600519.SH", "2025-01-01", "2025-06-30")
        assert calls == []
    finally:
        set_config(old, replace=True)


@pytest.mark.unit
def test_missing_financial_table_is_explicit_partial(monkeypatch):
    prepared = _resolved_config(monkeypatch)
    unavailable = {
        "data": {"status": "unavailable", "periods": [], "reason": "no PIT disclosure"},
        "meta": _meta(price_basis="not_applicable"),
    }
    monkeypatch.setattr(quantlab, "_client", lambda config: _FakeClient([unavailable]))
    old = get_config()
    try:
        set_config(prepared, replace=True)
        text = get_quantlab_fundamentals("600519.SH", "2025-06-30")
        provenance = provenance_for_config(prepared)
    finally:
        set_config(old, replace=True)

    assert text.startswith("DATA_UNAVAILABLE:")
    assert "do not fabricate" in text
    assert provenance["status"] == "partial"


@pytest.mark.unit
def test_preflight_requires_full_version_triple(monkeypatch):
    config = _base_config()
    config["snapshot_id"] = None
    monkeypatch.setattr(
        quantlab,
        "_client",
        lambda config: pytest.fail("transport must not start before config validation"),
    )
    with pytest.raises(QuantLabError, match="snapshot_id is required"):
        prepare_quantlab_run(config)


@pytest.mark.unit
def test_failed_preflight_writes_minimal_redacted_provenance(tmp_path):
    config = _base_config()
    config["results_dir"] = str(tmp_path)
    token = config["quantlab_api_token"]

    output = write_failed_provenance(
        config,
        QuantLabError("AUTH_FAILED", f"authorization rejected for {token}"),
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "AUTH_FAILED"
    assert payload["versions"] == {
        "snapshot_id": "snapshot-1",
        "financial_manifest_id": "manifest-1",
        "as_of_date": "2025-06-30",
    }
    assert payload["requests"] == [
        {
            "endpoint": "/context/resolve",
            "status": "failed",
            "error_code": "AUTH_FAILED",
            "message": "AUTH_FAILED: authorization rejected for [REDACTED]",
        }
    ]
    assert token not in output.read_text(encoding="utf-8")


@pytest.mark.unit
def test_failed_market_contract_is_recorded_in_provenance(monkeypatch):
    prepared = _resolved_config(monkeypatch)
    future_bars = {
        "data": {
            "bars": [
                {
                    "date": "2025-07-01",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
            ]
        },
        "meta": _meta(),
    }
    monkeypatch.setattr(quantlab, "_client", lambda config: _FakeClient([future_bars]))
    old = get_config()
    try:
        set_config(prepared, replace=True)
        with pytest.raises(QuantLabError, match="FUTURE_DATA"):
            quantlab.get_quantlab_stock("600519.SH", "2025-06-01", "2025-06-30")
        provenance = provenance_for_config(prepared)
    finally:
        set_config(old, replace=True)

    assert provenance["status"] == "failed"
    assert provenance["requests"][-1]["endpoint"] == "/market-bars"
    assert provenance["requests"][-1]["status"] == "failed"
    assert provenance["requests"][-1]["error_code"] == "FUTURE_DATA"
