"""REST API (§4.7) end to end on the synthetic database with the scripted LLM."""
import time
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from core import models as m
from core.settings import Settings, get_settings
from core.store import SqlStore
from orchestrator.budget import BudgetManager
from orchestrator.config import Config
from orchestrator.deps import Deps
from orchestrator.fakes import RecordingNotifier, ScriptedLLM
from orchestrator.ports import AllowlistedTools

from .synth import synthetic_db
from .test_tools import make_service


@pytest.fixture(scope="module")
def client():
    _, f = synthetic_db()

    @asynccontextmanager
    async def deps_cm():
        store = SqlStore(f)
        cfg = Config()
        yield Deps(cfg, AllowlistedTools(make_service(f)), store, ScriptedLLM(record_to=store), RecordingNotifier(),
                   BudgetManager(10, 10))

    from api.main import create_app

    with TestClient(create_app(deps_cm, f)) as c:
        c.f = f
        yield c


def wait_job(client, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/v1/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_health(client):
    h = client.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["last_price_date"] == "2026-10-12" and h["kill_switch"] is False


def test_analyze_job_with_progress_and_detail(client):
    r = client.post("/api/v1/analyze", json={"symbol": "UP4", "horizons": ["short"]})
    assert r.status_code == 202
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    kinds = [e["kind"] for e in job["events"]]
    assert kinds[0] == "started" and kinds[-1] == "done" and "progress" in kinds
    assert any("Short horizon" in e["message"] for e in job["events"])
    with client.stream("GET", f"/api/v1/jobs/{job['job_id']}/events") as s:
        body = "".join(s.iter_text())
    assert "event: done" in body and "event: result" in body
    reco_id = job["result"]["results"][0]["reco_id"]
    d = client.get(f"/api/v1/recommendations/{reco_id}").json()
    assert d["symbol"] == "UP4" and d["card"]["levels"]["stop"] == d["stop"] and d["evidence"]
    ev = client.get(f"/api/v1/evidence/{d['evidence'][0]['id']}").json()
    assert ev["payload"] and ev["as_of"]
    listed = client.get("/api/v1/recommendations", params={"symbol": "UP4"}).json()["items"]
    assert listed[0]["id"] == reco_id
    run = client.get(f"/api/v1/runs/{d['run_id']}").json()
    assert run["tool_calls"] and run["llm_calls"] and reco_id in run["recommendations"]


def test_analyze_validation(client):
    assert client.post("/api/v1/analyze", json={"symbol": "UP1", "horizons": ["weekly"]}).status_code == 422


def test_portfolio_and_idempotent_orders(client):
    pid = client.post("/api/v1/portfolios", json={"name": "Manual", "starting_cash": 1_000_000}).json()["id"]
    body = {"symbol": "UP1", "amount": 50_000, "stop": 400, "target": 900}
    assert client.post(f"/api/v1/portfolios/{pid}/orders", json=body).status_code == 400
    a = client.post(f"/api/v1/portfolios/{pid}/orders", json=body, headers={"Idempotency-Key": "abc"})
    b = client.post(f"/api/v1/portfolios/{pid}/orders", json=body, headers={"Idempotency-Key": "abc"})
    assert a.status_code == 201 and a.json()["order_id"] == b.json()["order_id"]
    bad = client.post(f"/api/v1/portfolios/{pid}/orders", json={"symbol": "UP1", "qty": 5, "stop": 900, "target": 400},
                      headers={"Idempotency-Key": "bad"})
    assert bad.status_code == 422 and bad.json()["code"] == "ORDER_REJECTED"
    assert len(client.get(f"/api/v1/portfolios/{pid}/orders").json()["items"]) == 1
    p = client.get(f"/api/v1/portfolios/{pid}").json()
    assert p["name"] == "Manual" and p["positions"] == []
    assert any(x["id"] == pid for x in client.get("/api/v1/portfolios").json()["items"])


def test_order_prefilled_from_recommendation(client):
    job = wait_job(client, client.post("/api/v1/analyze", json={"symbol": "UP5", "horizons": ["short"]}).json()["job_id"])
    rid = job["result"]["results"][0]["reco_id"]
    pid = client.post("/api/v1/portfolios", json={"name": "From card", "starting_cash": 500_000}).json()["id"]
    o = client.post(f"/api/v1/portfolios/{pid}/orders", json={"reco_id": rid, "amount": 20_000},
                    headers={"Idempotency-Key": f"reco-{rid}"}).json()
    card = client.get(f"/api/v1/recommendations/{rid}").json()
    assert o["symbol"] == "UP5" and o["stop"] == card["stop"] and o["target"] == card["target"]


def test_hitl_inbox_flow(client):
    job = wait_job(client, client.post("/api/v1/analyze", json={"symbol": "UP6", "horizons": ["short"]}).json()["job_id"])
    rid = job["result"]["results"][0]["reco_id"]
    tid = SqlStore(client.f).create_hitl_task("approve_reco", "recommendation", rid, {"card": {}}, "approve", "L1", 2)
    items = client.get("/api/v1/hitl/tasks").json()["items"]
    t = next(i for i in items if i["id"] == tid)
    assert "approve" in t["allowed_actions"] and t["due_at"]
    bad = client.post(f"/api/v1/hitl/tasks/{tid}/decision", json={"action": "close"})
    assert bad.status_code == 422
    ok = client.post(f"/api/v1/hitl/tasks/{tid}/decision", json={"action": "approve", "note": "fine"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert client.get(f"/api/v1/recommendations/{rid}").json()["status"] == "published"
    again = client.post(f"/api/v1/hitl/tasks/{tid}/decision", json={"action": "reject"})
    assert again.status_code == 409


def test_scorecard_runs_config_and_kill_switch(client):
    sc = client.get("/api/v1/scorecard").json()
    assert {"overall", "by_horizon", "calibration", "abstention", "cost", "portfolios"} <= set(sc)
    assert client.get("/api/v1/runs").json()["items"]
    assert client.post("/api/v1/admin/kill-switch", json={"enabled": True}).json() == {"kill_switch": True}
    assert client.get("/api/v1/config").json()["kill_switch"] is True
    assert client.get("/api/v1/health").json()["kill_switch"] is True
    client.post("/api/v1/admin/kill-switch", json={"enabled": False})
    with client.f() as s:
        assert s.query(m.ConfigVersion).filter(m.ConfigVersion.created_by == "kill_switch").count() == 2


def test_bearer_token_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("STOCK_PRIMER_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    try:
        assert client.get("/api/v1/recommendations").status_code == 401
        assert client.get("/api/v1/recommendations", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get("/api/v1/health").status_code == 200
    finally:
        monkeypatch.delenv("STOCK_PRIMER_API_TOKEN")
        get_settings.cache_clear()
