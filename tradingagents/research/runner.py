"""Execute one research run with isolated QuantLab version pins."""

from __future__ import annotations

import logging
import os
import uuid
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from tradingagents.dataflows.config import set_config
from tradingagents.default_config import DEFAULT_CONFIG, apply_env_overrides
from tradingagents.research.bundle_client import fetch_bundle
from tradingagents.research.compat import to_quantlab_report
from tradingagents.research.protocol import STATUS_COMPLETED, STATUS_FAILED, STATUS_PARTIAL
from tradingagents.research.schemas import ResearchBundle, StructuredReport
from tradingagents.research.store import ResearchRunStore
from tradingagents.research.synthesizer import LangChainSynthesizer, ResearchSynthesizer
from tradingagents.research.workflows import run_candidate_review, run_industry_thesis

logger = logging.getLogger(__name__)


def code_version() -> str:
    try:
        return version("tradingagents")
    except PackageNotFoundError:
        return "unknown"


def isolated_run_config(bundle: ResearchBundle, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pin versions from the frozen bundle. Never implicit-latest."""
    config = apply_env_overrides(deepcopy(base or DEFAULT_CONFIG))
    config["research_profile"] = "quantlab_a_share"
    config["snapshot_id"] = bundle.versions.snapshot_id
    config["financial_manifest_id"] = bundle.versions.financial_manifest_id
    config["as_of_date"] = bundle.as_of_date
    config["_research_bundle_id"] = bundle.bundle_id
    config["_research_as_of_time"] = bundle.as_of_time
    return config


def _llm_synthesizer(config: dict[str, Any]) -> tuple[ResearchSynthesizer, str, str | None]:
    from tradingagents.llm_clients.factory import create_llm_client

    provider = str(config.get("llm_provider") or "openai")
    model = str(config.get("deep_think_llm") or config.get("quick_think_llm") or "unknown")
    client = create_llm_client(
        provider,
        model,
        base_url=config.get("backend_url"),
    )
    llm = client.get_llm()
    return LangChainSynthesizer(llm, agent_name="research"), model, provider


def execute_bundle(
    bundle: ResearchBundle,
    *,
    run_id: str,
    synthesizer: ResearchSynthesizer | None = None,
    config: dict[str, Any] | None = None,
) -> StructuredReport:
    isolated = isolated_run_config(bundle, config)
    set_config(isolated, replace=True)
    provider = isolated.get("llm_provider")
    model = str(isolated.get("deep_think_llm") or "unknown")
    if synthesizer is None:
        synthesizer, model, provider = _llm_synthesizer(isolated)
    report_id = f"rpt_{uuid.uuid4().hex}"
    kwargs = {
        "run_id": run_id,
        "report_id": report_id,
        "synthesizer": synthesizer,
        "model": model,
        "code_version": code_version(),
        "llm_provider": provider,
    }
    if bundle.research_type == "industry_thesis":
        return run_industry_thesis(bundle, **kwargs)
    return run_candidate_review(bundle, **kwargs)


def execute_stored_run(
    run_id: str,
    store: ResearchRunStore,
    *,
    synthesizer: ResearchSynthesizer | None = None,
    fetch=fetch_bundle,
    config: dict[str, Any] | None = None,
) -> StructuredReport:
    run = store.get(run_id)
    if run is None:
        raise KeyError(run_id)
    cfg = apply_env_overrides(deepcopy(config or DEFAULT_CONFIG))
    logger.info("research run start run_id=%s type=%s bundle=%s", run_id, run.research_type, run.bundle_id)
    bundle = fetch(
        run.bundle_id,
        base_url=cfg.get("quantlab_base_url"),
        token=cfg.get("quantlab_api_token"),
        timeout=float(cfg.get("quantlab_timeout_seconds") or 15),
        retries=int(cfg.get("quantlab_max_retries") or 2),
    )
    if bundle.research_type != run.research_type:
        raise ValueError(
            f"bundle type {bundle.research_type} does not match task {run.research_type}"
        )
    report = execute_bundle(bundle, run_id=run_id, synthesizer=synthesizer, config=cfg)
    status = report.status
    if status == "incomplete":
        store_status = STATUS_FAILED
        retryable = False
        error = "; ".join(report.validation_errors) or "structured output failed validation"
    elif status == "partial":
        store_status = STATUS_PARTIAL
        retryable = False
        error = None
    elif status == "failed":
        store_status = STATUS_FAILED
        retryable = False
        error = "; ".join(report.validation_errors) or "research run failed"
    else:
        store_status = STATUS_COMPLETED
        retryable = False
        error = None
    store.update(
        run_id,
        status=store_status,
        retryable=retryable,
        error=error,
        result={
            "report": report.model_dump(mode="json"),
            "readable_markdown": report.readable_markdown,
            "wire_report": to_quantlab_report(report),
        },
        clear_pid=True,
    )
    logger.info(
        "research run finish run_id=%s status=%s errors=%s",
        run_id,
        store_status,
        report.validation_errors,
    )
    return report


def process_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
