"""Strict, point-in-time QuantLab data vendor for A-share research runs."""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from stockstats import wrap
from urllib3.util.retry import Retry

from ..report_store import build_report_store
from .config import get_config

PROFILE = "quantlab_a_share"
VENDOR = "quantlab_tushare"
_PROVENANCE: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()
_MARKET_CACHE: OrderedDict[tuple[str, str, str, str], pd.DataFrame] = OrderedDict()
_MARKET_CACHE_LOCK = threading.RLock()
_MARKET_CACHE_MAX_ENTRIES = 16


class QuantLabError(RuntimeError):
    """A redacted, stable failure raised by the QuantLab client or contract."""

    def __init__(self, code: str, message: str, details: Any = None):
        self.code = code
        self.details = details
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class QuantLabRunContext:
    profile: str
    base_url: str
    snapshot_id: str
    financial_manifest_id: str
    as_of_date: str
    target_ticker: str
    benchmark_ticker: str
    data_start_date: str
    data_end_date: str
    quantlab_revision: str
    tradingagents_revision: str

    @property
    def signature(self) -> str:
        return "|".join(
            (
                self.profile,
                self.snapshot_id,
                self.financial_manifest_id,
                self.as_of_date,
                self.target_ticker,
                self.benchmark_ticker,
            )
        )


def _profile(value: Any) -> str:
    return str(value or "default").strip().lower().replace("-", "_")


def is_quantlab_profile(config: dict[str, Any] | None = None) -> bool:
    return _profile((config or get_config()).get("research_profile")) == PROFILE


def _client(config: dict[str, Any]) -> QuantLabClient:
    return QuantLabClient(
        config.get("quantlab_base_url"),
        token=config.get("quantlab_api_token"),
        timeout=float(config.get("quantlab_timeout_seconds", 15)),
        retries=int(config.get("quantlab_max_retries", 2)),
    )


class QuantLabClient:
    def __init__(
        self, base_url: str | None, token: str | None = None, timeout: float = 15, retries: int = 2
    ):
        if not base_url:
            raise QuantLabError("CONFIG_MISSING", "quantlab_base_url is required")
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=max(0, retries),
            connect=max(0, retries),
            read=max(0, retries),
            status=max(0, retries),
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET", "POST")),
            backoff_factor=0.25,
            raise_on_status=False,
        )
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({"Accept": "application/json"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self.session.request(
                method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise QuantLabError("TRANSPORT_ERROR", "QuantLab request failed") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuantLabError(
                "INVALID_RESPONSE", f"QuantLab returned non-JSON HTTP {response.status_code}"
            ) from exc
        if not isinstance(payload, dict):
            raise QuantLabError("INVALID_RESPONSE", "QuantLab response must be a JSON object")
        if response.status_code >= 400 or "error" in payload:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            raise QuantLabError(
                str(error.get("code") or f"HTTP_{response.status_code}"),
                str(error.get("message") or "QuantLab rejected the request"),
                error.get("details"),
            )
        if "data" not in payload or not isinstance(payload.get("meta"), dict):
            raise QuantLabError("INVALID_RESPONSE", "QuantLab response is missing data/meta")
        meta = payload["meta"]
        required_meta = {"request_id", "provider", "build_revision", "versions", "data_range"}
        missing = sorted(required_meta.difference(meta))
        if missing:
            raise QuantLabError(
                "INVALID_RESPONSE", f"QuantLab response meta is missing: {', '.join(missing)}"
            )
        return payload


def _required(config: dict[str, Any], key: str) -> str:
    value = config.get(key)
    if value is None or str(value).strip() == "":
        raise QuantLabError("CONFIG_MISSING", f"{key} is required for quantlab-a-share")
    return str(value).strip()


def _request_params(context: QuantLabRunContext) -> dict[str, str]:
    return {
        "target_ticker": context.target_ticker,
        "snapshot_id": context.snapshot_id,
        "financial_manifest_id": context.financial_manifest_id,
        "as_of_date": context.as_of_date,
        "benchmark_ticker": context.benchmark_ticker,
    }


def _new_provenance(context: QuantLabRunContext, response_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "success",
        "profile": context.profile,
        "versions": {
            "snapshot_id": context.snapshot_id,
            "financial_manifest_id": context.financial_manifest_id,
            "as_of_date": context.as_of_date,
        },
        "target_ticker": context.target_ticker,
        "benchmark_ticker": context.benchmark_ticker,
        "actual_provider": "tushare",
        "data_adapter": VENDOR,
        "revisions": {
            "quantlab": context.quantlab_revision,
            "tradingagents": context.tradingagents_revision,
        },
        "price_basis": response_meta.get("price_basis", "hfq"),
        "analysts": ["market", "fundamentals"],
        "models": {},
        "requests": [],
        "created_at": datetime.now().astimezone().isoformat(),
    }


def _record(context: QuantLabRunContext, endpoint: str, payload: dict[str, Any]) -> None:
    meta = payload.get("meta", {})
    data = payload.get("data")
    item_status = data.get("status") if isinstance(data, dict) else "success"
    item = {
        "endpoint": endpoint,
        "status": item_status or "success",
        "request_id": meta.get("request_id"),
        "provider": meta.get("provider"),
        "data_range": meta.get("data_range"),
        "price_basis": meta.get("price_basis"),
    }
    with _LOCK:
        provenance = _PROVENANCE.setdefault(context.signature, _new_provenance(context, meta))
        provenance["requests"].append(item)
        if item_status == "unavailable":
            provenance["status"] = "partial"


def _redacted_error_message(exc: Exception, config: dict[str, Any]) -> str:
    message = str(exc)
    token = config.get("quantlab_api_token")
    if token:
        message = message.replace(str(token), "[REDACTED]")
    return message


def _record_failure(
    context: QuantLabRunContext,
    endpoint: str,
    exc: Exception,
    config: dict[str, Any],
) -> None:
    item = {
        "endpoint": endpoint,
        "status": "failed",
        "error_code": exc.code if isinstance(exc, QuantLabError) else "REQUEST_FAILED",
        "message": _redacted_error_message(exc, config),
    }
    with _LOCK:
        provenance = _PROVENANCE.setdefault(context.signature, _new_provenance(context, {}))
        provenance["requests"].append(item)
        provenance["status"] = "failed"


def _validate_bound_response(
    payload: dict[str, Any], context: QuantLabRunContext, *, price_basis: str | None = None
) -> None:
    """Reject data that is not bound to the immutable preflight context."""
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise QuantLabError("INVALID_RESPONSE", "QuantLab response meta must be an object")
    versions = meta.get("versions")
    if not isinstance(versions, dict):
        raise QuantLabError("INVALID_RESPONSE", "QuantLab response versions must be an object")
    expected = {
        "snapshot_id": context.snapshot_id,
        "financial_manifest_id": context.financial_manifest_id,
        "as_of_date": context.as_of_date,
    }
    actual = {key: str(versions.get(key) or "") for key in expected}
    if actual != expected:
        raise QuantLabError(
            "VERSION_MISMATCH",
            "QuantLab data response does not match the immutable run context",
            {"expected": expected, "actual": actual},
        )
    if str(meta.get("provider") or "").lower() != "tushare":
        raise QuantLabError("PROVIDER_MISMATCH", "QuantLab response is not backed by Tushare")
    if price_basis is not None and str(meta.get("price_basis") or "").lower() != price_basis:
        raise QuantLabError(
            "PRICE_BASIS_MISMATCH", f"QuantLab response must use {price_basis} prices"
        )


def prepare_quantlab_run(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and bind one immutable QuantLab context before LLM construction."""
    prepared = deepcopy(config)
    if not is_quantlab_profile(prepared):
        return prepared
    base_url = _required(prepared, "quantlab_base_url")
    snapshot_id = _required(prepared, "snapshot_id")
    manifest_id = _required(prepared, "financial_manifest_id")
    as_of_date = _required(prepared, "as_of_date")
    try:
        parsed_as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise QuantLabError("INVALID_DATE", "as_of_date must use YYYY-MM-DD") from exc
    if parsed_as_of > datetime.now().date():
        raise QuantLabError("FUTURE_DATE", "as_of_date cannot be in the future")
    target = _required(prepared, "quantlab_target_ticker")
    benchmark = str(prepared.get("benchmark_ticker") or "000300.SH").strip()
    request_data = {
        "ticker": target,
        "snapshot_id": snapshot_id,
        "financial_manifest_id": manifest_id,
        "as_of_date": as_of_date,
        "benchmark_ticker": benchmark,
    }
    response = _client(prepared).request(
        "POST", "/api/research-data/v1/context/resolve", json=request_data
    )
    data = response["data"]
    if not isinstance(data, dict) or not data.get("ticker"):
        raise QuantLabError("INVALID_RESPONSE", "context/resolve returned an invalid context")
    meta = response["meta"]
    versions = meta.get("versions") if isinstance(meta.get("versions"), dict) else {}
    actual = {
        "snapshot_id": str(data.get("snapshot_id") or versions.get("snapshot_id") or ""),
        "financial_manifest_id": str(
            data.get("financial_manifest_id") or versions.get("financial_manifest_id") or ""
        ),
        "as_of_date": str(data.get("as_of_date") or versions.get("as_of_date") or ""),
    }
    requested = {
        "snapshot_id": snapshot_id,
        "financial_manifest_id": manifest_id,
        "as_of_date": as_of_date,
    }
    if actual != requested:
        raise QuantLabError(
            "VERSION_MISMATCH",
            "QuantLab resolved a context different from the requested version triple",
            {"requested": requested, "actual": actual},
        )
    if str(meta.get("provider") or "").lower() != "tushare":
        raise QuantLabError("PROVIDER_MISMATCH", "QuantLab context is not backed by Tushare")
    resolved_benchmark = str(data.get("benchmark_ticker") or "")
    if resolved_benchmark != benchmark:
        raise QuantLabError("VERSION_MISMATCH", "QuantLab resolved a different benchmark")
    data_range = meta.get("data_range")
    if (
        not isinstance(data_range, dict)
        or not data_range.get("start_date")
        or not data_range.get("end_date")
    ):
        raise QuantLabError("INVALID_RESPONSE", "context/resolve is missing the frozen data range")
    context = QuantLabRunContext(
        profile=PROFILE,
        base_url=base_url.rstrip("/"),
        snapshot_id=actual["snapshot_id"],
        financial_manifest_id=actual["financial_manifest_id"],
        as_of_date=actual["as_of_date"],
        target_ticker=str(data["ticker"]),
        benchmark_ticker=resolved_benchmark,
        data_start_date=str(data_range["start_date"]),
        data_end_date=str(data_range["end_date"]),
        quantlab_revision=str(meta.get("build_revision") or "unknown"),
        tradingagents_revision=str(
            prepared.get("tradingagents_revision")
            or os.environ.get("TRADINGAGENTS_BUILD_REVISION")
            or "unknown"
        ),
    )
    prepared["research_profile"] = PROFILE
    prepared["_quantlab_requested_ticker"] = target
    prepared["snapshot_id"] = context.snapshot_id
    prepared["financial_manifest_id"] = context.financial_manifest_id
    prepared["as_of_date"] = context.as_of_date
    prepared["quantlab_target_ticker"] = context.target_ticker
    prepared["benchmark_ticker"] = context.benchmark_ticker
    prepared["_quantlab_context"] = asdict(context)
    vendors = deepcopy(prepared.get("data_vendors", {}))
    for category in ("core_stock_apis", "technical_indicators", "fundamental_data"):
        vendors[category] = VENDOR
    prepared["data_vendors"] = vendors
    tool_vendors = deepcopy(prepared.get("tool_vendors", {}))
    for tool in (
        "get_stock_data",
        "get_indicators",
        "get_fundamentals",
        "get_balance_sheet",
        "get_cashflow",
        "get_income_statement",
    ):
        tool_vendors[tool] = VENDOR
    prepared["tool_vendors"] = tool_vendors
    with _LOCK:
        _PROVENANCE[context.signature] = _new_provenance(context, meta)
    with _MARKET_CACHE_LOCK:
        _MARKET_CACHE.clear()
    _record(context, "/context/resolve", response)
    return prepared


def _context() -> tuple[dict[str, Any], QuantLabRunContext, QuantLabClient]:
    config = get_config()
    raw = config.get("_quantlab_context")
    if not isinstance(raw, dict):
        raise QuantLabError("CONTEXT_NOT_BOUND", "QuantLab run context was not preflighted")
    context = QuantLabRunContext(**raw)
    return config, context, _client(config)


def resolve_quantlab_identity(ticker: str) -> dict[str, Any]:
    config, context, client = _context()
    try:
        payload = client.request(
            "GET",
            f"/api/research-data/v1/securities/{quote(ticker, safe='')}",
            params=_request_params(context),
        )
        _validate_bound_response(payload, context)
        data = payload["data"]
        if not isinstance(data, dict) or not data.get("ts_code"):
            raise QuantLabError("INVALID_RESPONSE", "securities response is missing ts_code")
        if str(data.get("asset_type") or "stock").lower() not in {"stock", "a_share"}:
            raise QuantLabError("UNSUPPORTED_ASSET", "resolved security is not an A-share stock")
        result = {
            "requested_symbol": ticker,
            "canonical_symbol": data["ts_code"],
            "company_name": data.get("name") or data["ts_code"],
            "asset_type": "stock",
            "exchange": data.get("exchange"),
            "currency": "CNY",
        }
        _record(context, "/securities", payload)
        return result
    except Exception as exc:
        _record_failure(context, "/securities", exc, config)
        raise


def _market_frame(
    ticker: str, start_date: str | None = None, end_date: str | None = None
) -> pd.DataFrame:
    config, context, client = _context()
    cache_key = (
        context.signature,
        str(ticker).upper(),
        str(start_date or ""),
        str(end_date or ""),
    )
    # Keep the request inside the lock so concurrent indicator tools share one
    # immutable snapshot read instead of racing through identical Parquet scans.
    with _MARKET_CACHE_LOCK:
        cached = _MARKET_CACHE.get(cache_key)
        if cached is not None:
            _MARKET_CACHE.move_to_end(cache_key)
            return cached.copy(deep=True)
        try:
            params = _request_params(context)
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date
            payload = client.request(
                "GET",
                f"/api/research-data/v1/market-bars/{quote(ticker, safe='')}",
                params=params,
            )
            _validate_bound_response(payload, context, price_basis="hfq")
            rows = payload["data"].get("bars") if isinstance(payload["data"], dict) else None
            if not isinstance(rows, list) or not rows:
                raise QuantLabError("DATA_NOT_FOUND", f"No frozen market bars for {ticker}")
            frame = pd.DataFrame(rows)
            rename = {
                name: name.title() for name in ("date", "open", "high", "low", "close", "volume")
            }
            frame = frame.rename(columns=rename)
            required = {"Date", "Open", "High", "Low", "Close", "Volume"}
            if not required.issubset(frame.columns):
                raise QuantLabError("INVALID_RESPONSE", "market bars are missing OHLCV fields")
            frame["Date"] = pd.to_datetime(frame["Date"], errors="raise")
            if (
                frame["Date"].dt.date.max()
                > datetime.strptime(context.as_of_date, "%Y-%m-%d").date()
            ):
                raise QuantLabError("FUTURE_DATA", "market bars contain data after as_of_date")
            result = frame.sort_values("Date").reset_index(drop=True)
            _record(context, "/market-bars", payload)
            _MARKET_CACHE[cache_key] = result.copy(deep=True)
            _MARKET_CACHE.move_to_end(cache_key)
            while len(_MARKET_CACHE) > _MARKET_CACHE_MAX_ENTRIES:
                _MARKET_CACHE.popitem(last=False)
            return result
        except Exception as exc:
            _record_failure(context, "/market-bars", exc, config)
            raise


def load_quantlab_ohlcv(
    ticker: str, start_date: str | None = None, end_date: str | None = None
) -> pd.DataFrame:
    """Load the bound frozen OHLCV frame for deterministic local calculations."""
    return _market_frame(ticker, start_date, end_date)


def get_quantlab_stock(ticker: str, start_date: str, end_date: str) -> str:
    frame = _market_frame(ticker, start_date, end_date)
    return (
        f"# QuantLab frozen Tushare market data for {ticker} from {start_date} to {end_date}\n"
        f"# Total records: {len(frame)}; price basis: hfq\n\n" + frame.to_csv(index=False)
    )


def get_quantlab_indicators(
    ticker: str, indicator: str, curr_date: str, look_back_days: int
) -> str:
    supported = {
        "close_50_sma",
        "close_200_sma",
        "close_10_ema",
        "macd",
        "macds",
        "macdh",
        "rsi",
        "boll",
        "boll_ub",
        "boll_lb",
        "atr",
        "vwma",
        "mfi",
    }
    if indicator not in supported:
        raise ValueError(f"Indicator {indicator} is not supported; choose from {sorted(supported)}")
    end = datetime.strptime(curr_date, "%Y-%m-%d")
    start = end - timedelta(days=max(int(look_back_days), 400))
    _, context, _ = _context()
    start = max(start, datetime.strptime(context.data_start_date, "%Y-%m-%d"))
    frame = _market_frame(ticker, start.strftime("%Y-%m-%d"), curr_date)
    stock = wrap(frame.set_index("Date").rename(columns=str.lower))
    stock[indicator]
    cutoff = pd.Timestamp(end - timedelta(days=int(look_back_days)))
    values = stock.loc[stock.index >= cutoff, indicator]
    lines = [
        f"{date:%Y-%m-%d}: {'N/A' if pd.isna(value) else value}" for date, value in values.items()
    ]
    return f"## {indicator} from QuantLab frozen OHLCV\n\n" + "\n".join(lines)


_FINANCIAL_PATHS = {
    "indicators": "indicators",
    "balance-sheet": "balance-sheet",
    "cash-flow": "cash-flow",
    "income": "income",
}


def _financial(ticker: str, statement: str, curr_date: str | None = None) -> str:
    config, context, client = _context()
    if curr_date and str(curr_date) != context.as_of_date:
        raise QuantLabError(
            "DATE_MISMATCH", "financial tool date must equal the immutable QuantLab as_of_date"
        )
    try:
        payload = client.request(
            "GET",
            f"/api/research-data/v1/financials/{quote(ticker, safe='')}/{_FINANCIAL_PATHS[statement]}",
            params={**_request_params(context), "limit": 4},
        )
        _validate_bound_response(payload, context)
        data = payload["data"]
        if not isinstance(data, dict) or data.get("status") not in {"available", "unavailable"}:
            raise QuantLabError("INVALID_RESPONSE", "financial response has an invalid status")
        if data.get("status") == "unavailable":
            _record(context, f"/financials/{statement}", payload)
            return (
                f"DATA_UNAVAILABLE: QuantLab {statement} is unavailable for {ticker} "
                f"as of {context.as_of_date}. Proceed without it; do not fabricate or use another vendor."
            )
        periods = data.get("periods")
        if not isinstance(periods, list):
            raise QuantLabError("INVALID_RESPONSE", "financial response periods must be a list")
        for period in periods:
            if not isinstance(period, dict) or not period.get("available_at"):
                raise QuantLabError("INVALID_RESPONSE", "financial period is missing available_at")
            available = str(period["available_at"])[:10]
            if available > context.as_of_date:
                raise QuantLabError(
                    "FUTURE_DATA", "financial response contains a future disclosure"
                )
        _record(context, f"/financials/{statement}", payload)
        return (
            f"# QuantLab frozen Tushare {statement} for {ticker}\n"
            f"# as_of_date={context.as_of_date}; manifest={context.financial_manifest_id}\n\n"
            + json.dumps(periods, ensure_ascii=False, indent=2, default=str)
        )
    except Exception as exc:
        _record_failure(context, f"/financials/{statement}", exc, config)
        raise


def get_quantlab_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    return _financial(ticker, "indicators", curr_date)


def get_quantlab_balance_sheet(
    ticker: str, freq: str = "quarterly", curr_date: str | None = None
) -> str:
    return _financial(ticker, "balance-sheet", curr_date)


def get_quantlab_cashflow(
    ticker: str, freq: str = "quarterly", curr_date: str | None = None
) -> str:
    return _financial(ticker, "cash-flow", curr_date)


def get_quantlab_income_statement(
    ticker: str, freq: str = "quarterly", curr_date: str | None = None
) -> str:
    return _financial(ticker, "income", curr_date)


def provenance_for_config(
    config: dict[str, Any], status: str | None = None
) -> dict[str, Any] | None:
    raw = config.get("_quantlab_context")
    if not isinstance(raw, dict):
        return None
    context = QuantLabRunContext(**raw)
    with _LOCK:
        value = deepcopy(_PROVENANCE.get(context.signature) or _new_provenance(context, {}))
    if status:
        value["status"] = status
    value["models"] = {
        "provider": config.get("llm_provider"),
        "quick": config.get("quick_think_llm"),
        "deep": config.get("deep_think_llm"),
    }
    return value


def write_failed_provenance(config: dict[str, Any], exc: Exception) -> Path:
    target = str(config.get("quantlab_target_ticker") or "unknown")
    path = (
        Path(config.get("results_dir") or ".")
        / target
        / str(config.get("as_of_date") or "unknown")
        / "reports"
    )
    path.mkdir(parents=True, exist_ok=True)
    code = exc.code if isinstance(exc, QuantLabError) else "PREFLIGHT_FAILED"
    payload = provenance_for_config(config) or {
        "schema_version": "1.0",
        "status": "failed",
        "profile": PROFILE,
        "versions": {
            "snapshot_id": config.get("snapshot_id"),
            "financial_manifest_id": config.get("financial_manifest_id"),
            "as_of_date": config.get("as_of_date"),
        },
        "target_ticker": target,
        "benchmark_ticker": config.get("benchmark_ticker") or "000300.SH",
        "actual_provider": "tushare",
        "data_adapter": VENDOR,
        "requests": [],
    }
    payload["status"] = "failed"
    message = _redacted_error_message(exc, config)
    payload["error"] = {"code": code, "message": message}
    if not payload["requests"]:
        payload["requests"].append(
            {
                "endpoint": "/context/resolve",
                "status": "failed",
                "error_code": code,
                "message": message,
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    store = build_report_store(config)
    destination = (
        f"{target}/{config.get('as_of_date') or 'unknown'}/reports"
        if store.backend == "s3"
        else path
    )
    result = store.save({"provenance.json": encoded}, destination=destination)
    local = result.local_path("provenance.json")
    if local is not None:
        return local
    path.mkdir(parents=True, exist_ok=True)
    output = path / "provenance.json"
    output.write_text(encoded, encoding="utf-8")
    return output
