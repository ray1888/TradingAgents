"""Fetch a frozen research bundle from QuantLab. Never mutates upstream data."""

from __future__ import annotations

from typing import Any

from tradingagents.dataflows.quantlab_tushare import QuantLabClient, QuantLabError
from tradingagents.research.schemas import ResearchBundle


def parse_bundle_payload(payload: dict[str, Any]) -> ResearchBundle:
    if "schema_version" in payload and "bundle_id" in payload:
        return ResearchBundle.model_validate(payload)
    data = payload.get("data")
    if isinstance(data, dict) and "bundle_id" in data:
        return ResearchBundle.model_validate(data)
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
    api = client or QuantLabClient(base_url, token=token, timeout=timeout, retries=retries)
    payload = api.request("GET", f"/api/research-data/v1/bundles/{bundle_id}")
    bundle = parse_bundle_payload(payload)
    if bundle.bundle_id != bundle_id:
        raise QuantLabError(
            "BUNDLE_MISMATCH",
            f"requested bundle {bundle_id} but payload is {bundle.bundle_id}",
        )
    return bundle
