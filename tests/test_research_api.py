from fastapi.testclient import TestClient

from tests.research_fixtures import hog_bundle, hog_synthesizer, load_contract
from tradingagents.research.runner import execute_stored_run
from tradingagents.research.service import create_app, recover_orphans
from tradingagents.research.store import ResearchRunStore


def _app(tmp_path, bundles, synthesizer=None):
    store = ResearchRunStore(tmp_path / "runs.sqlite")
    syn = synthesizer or hog_synthesizer()

    def fetch(bundle_id, **kwargs):
        return bundles[bundle_id]

    def dispatch(st: ResearchRunStore) -> None:
        for run in st.list_queued():
            st.update(run.run_id, status="running", pid=None)
            execute_stored_run(
                run.run_id,
                st,
                synthesizer=syn,
                fetch=fetch,
            )

    return create_app(store=store, dispatch=dispatch), store


def test_submit_status_and_result_roundtrip(tmp_path):
    bundle = hog_bundle()
    app, _store = _app(tmp_path, {bundle.bundle_id: bundle})
    client = TestClient(app)
    caps = client.get("/api/research/v1/capabilities")
    assert caps.status_code == 200
    assert "quantlab_api_token" in caps.json()["config_status"]
    assert "OPENAI" not in str(caps.json())

    submitted = client.post("/api/research/v1/runs", json=load_contract("submit-industry.json"))
    assert submitted.status_code == 202
    run_id = submitted.json()["run_id"]
    status = client.get(f"/api/research/v1/runs/{run_id}")
    assert status.json()["status"] == "completed"
    result = client.get(f"/api/research/v1/runs/{run_id}/result")
    body = result.json()
    assert body["report"]["schema_version"] == "1"
    assert body["report"]["industry_thesis"]["expectation_gap_status"] == "pending_verification"
    assert body["readable_markdown"]


def test_idempotent_submit_and_conflict(tmp_path):
    bundle = hog_bundle()
    app, store = _app(tmp_path, {bundle.bundle_id: bundle})
    client = TestClient(app)
    payload = load_contract("submit-industry.json")
    first = client.post("/api/research/v1/runs", json=payload)
    second = client.post("/api/research/v1/runs", json=payload)
    assert first.json()["run_id"] == second.json()["run_id"]
    payload["bundle_id"] = "bundle-other"
    conflict = client.post("/api/research/v1/runs", json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert store.get(first.json()["run_id"]) is not None


def test_auth_required_when_token_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RESEARCH_API_TOKEN", "secret")
    bundle = hog_bundle()
    app, _store = _app(tmp_path, {bundle.bundle_id: bundle})
    client = TestClient(app)
    denied = client.get("/api/research/v1/capabilities")
    assert denied.status_code == 401
    ok = client.get(
        "/api/research/v1/capabilities",
        headers={"Authorization": "Bearer secret"},
    )
    assert ok.status_code == 200


def test_lost_process_is_retryable_failure(tmp_path):
    store = ResearchRunStore(tmp_path / "runs.sqlite")
    run = store.create_or_get(
        run_id="run_dead",
        schema_version="1",
        research_type="industry_thesis",
        bundle_id="bundle-hog-2026-09-08",
        idempotency_key="dead",
        request_hash="abc",
        input_summary={},
    )
    store.update(run.run_id, status="running", pid=99999999)
    recover_orphans(store)
    updated = store.get(run.run_id)
    assert updated.status == "failed"
    assert updated.retryable is True
