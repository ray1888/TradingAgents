"""Fetch a frozen research bundle from QuantLab. Never mutates upstream data."""

from __future__ import annotations

from typing import Any

from tradingagents.dataflows.quantlab_tushare import QuantLabClient, QuantLabError
from tradingagents.research.compat import normalize_bundle
from tradingagents.research.schemas import ResearchBundle


def parse_bundle_payload(payload: dict[str, Any]) -> ResearchBundle:
    if isinstance(payload.get("data"), dict) and (
        "bundle_id" in payload["data"] or "research_type" in payload["data"]
    ):
        return normalize_bundle(payload["data"])
    if "schema_version" in payload or "research_type" in payload:
        return normalize_bundle(payload)
    raise QuantLabError("INVALID_RESPONSE", "research bundle payload is missing bundle_id")


def fetch_bundle(
    bundle_id: str,
    *,
    base_url: str | None,
    token: str | None = None,
    timeout: float = 15,
    retries: int = 2,
    client: QuantLabClient | None = None,
) -> ResearchBundle:
    if client is not None:
        payload = client.request("GET", f"/api/research-data/v1/bundles/{bundle_id}")
    else:
        if not base_url:
            raise QuantLabError("CONFIG_MISSING", "quantlab_base_url is required")
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = requests.Session()
        retry = Retry(
            total=max(0, retries),
            connect=max(0, retries),
            read=max(0, retries),
            status=max(0, retries),
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET",)),
            backoff_factor=0.25,
            raise_on_status=False,
        )
        session.mount("http://", HTTPAdapter(max_retries=retry))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = session.request(
                "GET",
                f"{str(base_url).rstrip('/')}/api/research-data/v1/bundles/{bundle_id}",
                timeout=timeout,
                headers=headers,
            )
            if response.status_code == 404:
                response = session.request(
                    "GET",
                    f"{str(base_url).rstrip('/')}/api/agent-research/v1/bundles/{bundle_id}",
                    timeout=timeout,
                    headers=headers,
                )
        except requests.RequestException as exc:
            raise QuantLabError("TRANSPORT_ERROR", "QuantLab bundle request failed") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuantLabError(
                "INVALID_RESPONSE", f"QuantLab returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else {}
            raise QuantLabError(
                str((error or {}).get("code") or f"HTTP_{response.status_code}"),
                str((error or {}).get("message") or "QuantLab rejected the bundle request"),
            )
        if not isinstance(payload, dict):
            raise QuantLabError("INVALID_RESPONSE", "QuantLab bundle must be a JSON object")
    bundle = parse_bundle_payload(payload)
    if bundle.bundle_id and bundle.bundle_id != bundle_id:
        raise QuantLabError(
            "BUNDLE_MISMATCH",
            f"requested bundle {bundle_id} but payload is {bundle.bundle_id}",
        )
    if not bundle.bundle_id:
        bundle = bundle.model_copy(update={"bundle_id": bundle_id})
    return bundle
