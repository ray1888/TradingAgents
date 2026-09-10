"""HTTP research-task service. Submit returns 202; analysis runs asynchronously."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from tradingagents.default_config import DEFAULT_CONFIG, apply_env_overrides
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.research.protocol import (
    ANALYSIS_CONCURRENCY,
    MAX_CANDIDATES,
    PROTOCOL_VERSION,
    RESEARCH_TYPES,
    SCHEMA_VERSION,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
)
from tradingagents.research.runner import process_is_alive
from tradingagents.research.schemas import RunSubmitRequest
from tradingagents.research.store import IdempotencyConflict, ResearchRunStore

logger = logging.getLogger(__name__)

_DISPATCH_LOCK = threading.Lock()
_WORKER_PROC: subprocess.Popen | None = None


def default_db_path() -> Path:
    root = Path(
        os.environ.get(
            "TRADINGAGENTS_RESEARCH_DB",
            os.path.join(Path.home(), ".tradingagents", "research", "runs.sqlite"),
        )
    )
    return root


def request_hash(payload: RunSubmitRequest) -> str:
    canonical = json.dumps(
        {
            "schema_version": payload.schema_version,
            "research_type": payload.research_type,
            "bundle_id": payload.bundle_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _auth_token() -> str | None:
    return os.environ.get("TRADINGAGENTS_RESEARCH_API_TOKEN") or None


def require_service_auth(
    authorization: str | None = Header(default=None),
) -> None:
    expected = _auth_token()
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid bearer token")


def capabilities() -> dict[str, Any]:
    cfg = apply_env_overrides(dict(DEFAULT_CONFIG))
    provider = str(cfg.get("llm_provider") or "")
    key_name = get_api_key_env(provider)
    if key_name:
        llm_configured = bool(os.environ.get(key_name, ""))
    else:
        # Providers with no key env (ollama, bedrock) still count as configured
        # once a provider is selected.
        llm_configured = bool(provider)
    ready = bool(cfg.get("quantlab_base_url") and llm_configured)
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "schema_versions": [SCHEMA_VERSION],
        "research_types": list(RESEARCH_TYPES),
        "supported_research_types": list(RESEARCH_TYPES),
        "max_candidates": MAX_CANDIDATES,
        "analysis_concurrency": ANALYSIS_CONCURRENCY,
        "configuration": {"ready": ready},
        "config_status": {
            "quantlab_base_url": bool(cfg.get("quantlab_base_url")),
            "quantlab_api_token": bool(cfg.get("quantlab_api_token")),
            "llm_configured": llm_configured,
            "research_api_auth": bool(_auth_token()),
        },
    }


def recover_orphans(store: ResearchRunStore) -> None:
    for run in store.list_running():
        if not process_is_alive(run.pid):
            logger.error("research run orphaned run_id=%s pid=%s", run.run_id, run.pid)
            store.update(
                run.run_id,
                status=STATUS_FAILED,
                retryable=True,
                error="analysis process lost; retryable",
                clear_pid=True,
            )


def _worker_command(run_id: str, db_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "tradingagents.research.worker",
        "--run-id",
        run_id,
        "--db",
        str(db_path),
    ]


def dispatch_next(store: ResearchRunStore) -> None:
    global _WORKER_PROC
    with _DISPATCH_LOCK:
        if _WORKER_PROC is not None and _WORKER_PROC.poll() is None:
            return
        queued = store.list_queued()
        if not queued:
            _WORKER_PROC = None
            return
        run = queued[0]
        proc = subprocess.Popen(_worker_command(run.run_id, store.path))
        _WORKER_PROC = proc
        store.update(run.run_id, status=STATUS_RUNNING, pid=proc.pid, retryable=False, error=None)
        logger.info("research worker started run_id=%s pid=%s", run.run_id, proc.pid)

        def _reap() -> None:
            proc.wait()
            if proc.returncode not in (0, None):
                current = store.get(run.run_id)
                if current and current.status == STATUS_RUNNING:
                    store.update(
                        run.run_id,
                        status=STATUS_FAILED,
                        retryable=True,
                        error=f"analysis process exited {proc.returncode}",
                        clear_pid=True,
                    )
            dispatch_next(store)

        threading.Thread(target=_reap, name=f"research-reap-{run.run_id}", daemon=True).start()


def create_app(
    store: ResearchRunStore | None = None,
    dispatch=None,
) -> FastAPI:
    app = FastAPI(title="TradingAgents Research API", version=PROTOCOL_VERSION)
    app.state.store = store or ResearchRunStore(default_db_path())
    app.state.dispatch = dispatch or dispatch_next
    recover_orphans(app.state.store)

    @app.get("/api/research/v1/capabilities")
    def get_capabilities(_: None = Depends(require_service_auth)):
        return capabilities()

    @app.post("/api/research/v1/runs", status_code=202)
    def submit_run(payload: dict[str, Any], _: None = Depends(require_service_auth)):
        try:
            request = RunSubmitRequest.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        digest = request_hash(request)
        try:
            run = app.state.store.create_or_get(
                run_id=f"run_{uuid.uuid4().hex}",
                schema_version=request.schema_version,
                research_type=request.research_type,
                bundle_id=request.bundle_id,
                idempotency_key=request.idempotency_key,
                request_hash=digest,
                input_summary={
                    "research_type": request.research_type,
                    "bundle_id": request.bundle_id,
                    "schema_version": request.schema_version,
                },
            )
        except IdempotencyConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": str(exc),
                    "run_id": exc.run_id,
                },
            ) from exc
        if run.status == STATUS_QUEUED:
            app.state.dispatch(app.state.store)
        return {"run_id": run.run_id, "status": run.status}

    @app.get("/api/research/v1/runs/{run_id}")
    def get_run(run_id: str, _: None = Depends(require_service_auth)):
        run = app.state.store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.status == STATUS_RUNNING and not process_is_alive(run.pid):
            run = app.state.store.update(
                run_id,
                status=STATUS_FAILED,
                retryable=True,
                error="analysis process lost; retryable",
                clear_pid=True,
            )
        return run.public_status()

    @app.get("/api/research/v1/runs/{run_id}/result")
    def get_result(run_id: str, _: None = Depends(require_service_auth)):
        run = app.state.store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.result is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "RESULT_NOT_READY", "status": run.status, "run_id": run_id},
            )
        from tradingagents.research.compat import to_quantlab_report
        from tradingagents.research.schemas import StructuredReport

        stored = run.result.get("wire_report")
        if stored is None and run.result.get("report"):
            stored = to_quantlab_report(StructuredReport.model_validate(run.result["report"]))
        if stored is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "RESULT_NOT_READY", "status": run.status, "run_id": run_id},
            )
        return stored

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            body = {"error": detail}
        else:
            body = {"error": {"code": f"HTTP_{exc.status_code}", "message": str(detail)}}
        return JSONResponse(status_code=exc.status_code, content=body)

    return app


def main() -> None:
    import uvicorn

    listen = os.environ.get("TRADINGAGENTS_RESEARCH_LISTEN", "0.0.0.0:8090")
    host, _, port = listen.partition(":")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(
        "tradingagents.research.service:create_app",
        factory=True,
        host=host or "0.0.0.0",
        port=int(port or "8090"),
        workers=1,
    )


if __name__ == "__main__":
    main()
