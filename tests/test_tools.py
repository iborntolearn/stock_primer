"""Tool layer (§5): envelope, evidence, audit, allowlists, point-in-time news, MCP transport,
and the orchestrator running end to end on real tools over a synthetic database."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from core import models as m
from core.settings import Settings
from core.store import SqlStore
from ingestion.adapters.base import TokenBucket
from ingestion.adapters.gnews import GoogleNews
from mcp_servers.registry import ToolContext
from mcp_servers.server import build_server
from mcp_servers.service import ToolService
from orchestrator.budget import BudgetManager
from orchestrator.config import TOOL_ALLOWLIST, Autonomy, Config
from orchestrator.deps import Deps
from orchestrator.fakes import RecordingNotifier, ScriptedLLM
from orchestrator.graphs import analyze, run_daily
from orchestrator.mcp_gateway import McpToolGateway
from orchestrator.ports import AllowlistedTools
from orchestrator.replay import replay_analysis
from quant.engine import QuantEngine

from .synth import DECISION, rss, synthetic_db

UTC = timezone.utc


def make_service(f, news_items=None):
    settings = Settings(universe="NIFTY200")
    news = None
    if news_items is not None:
        news = GoogleNews(getter=lambda url: rss(news_items), bucket=TokenBucket(1000, 1000))
    return ToolService(ToolContext(factory=f, quant=QuantEngine(f, settings), settings=settings, news=news), SqlStore(f))


@pytest.fixture(scope="module")
def db():
    return synthetic_db()


def call(svc, role, tool, args, run_id=None, as_of=DECISION):
    return asyncio.run(svc.call(role, tool, args, run_id=run_id, as_of=as_of))


def count(f, model, *where):
    with f() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def test_envelope_evidence_and_audit(db):
    _, f = db
    svc = make_service(f)
    r = call(svc, "technical", "get_price_history", {"symbol": "UP0", "from_date": "2026-09-01"})
    assert r.ok and r.meta.evidence_id and r.meta.kind == "price_series"
    assert r.data["rows"][-1]["date"] == "2026-10-12" and len(r.data["rows"]) <= 400
    with f() as s:
        e = s.get(m.Evidence, r.meta.evidence_id)
        assert e.payload["symbol"] == "UP0"
        tc = s.scalars(select(m.ToolCall).where(m.ToolCall.result_ref == e.id)).one()
        assert tc.ok and tc.args_hash and tc.agent == "technical"


def test_allowlist_enforced_server_side(db):
    svc = make_service(db[1])
    r = call(svc, "technical", "get_financials", {"symbol": "UP0"})
    assert not r.ok and r.error.code == "FORBIDDEN"
    r = call(svc, "qa", "place_paper_order", {})
    assert not r.ok and r.error.code in ("FORBIDDEN", "NOT_FOUND")


def test_errors_are_envelopes(db):
    svc = make_service(db[1])
    assert call(svc, "technical", "get_price_history", {"symbol": "NOPE"}).error.code == "NOT_FOUND"
    assert call(svc, "orchestrator", "score_horizon", {"symbol": "UP0", "horizon": "weekly"}).error.code == "INVALID_ARGUMENT"
    assert call(svc, "orchestrator", "score_horizon", {"symbol": "UP0"}).error.code == "INVALID_ARGUMENT"
    r = call(svc, "fundamental", "get_ratios", {"symbol": "UP0"})
    assert r.error.code == "NOT_FOUND" and "no published quarterly results" in r.error.message


def test_quant_contract_via_tools(db):
    svc = make_service(db[1])
    screen = call(svc, "orchestrator", "screen_universe", {"horizon": "short", "top_k": 3}).data
    assert len(screen["candidates"]) == 3 and screen["regime"] in ("risk_on", "neutral", "risk_off")
    row = screen["candidates"][0]
    assert row["sector_index"] == "NIFTY IT"
    lv = call(svc, "orchestrator", "propose_levels", {"symbol": row["symbol"], "horizon": "short"}).data
    assert lv["stop"] < lv["entry_low"] <= lv["entry_high"] < lv["target"]
    gate = call(svc, "orchestrator", "data_quality_status", {}).data
    assert gate["passed"] is True


def test_news_is_untrusted_and_point_in_time(db):
    _, f = db
    now = datetime.now(UTC)
    svc = make_service(f, news_items=[("UP1 wins big order", "Mint", now - timedelta(days=1)),
                                      ("UP1 future story", "Mint", now + timedelta(days=1))])
    r = call(svc, "news", "get_stock_news", {"symbol": "UP1"}, as_of=now)
    assert r.ok and r.meta.untrusted and r.meta.kind == "news"
    titles = [i["title"] for i in r.data["items"]]
    assert titles == ["UP1 wins big order"]          # publisher suffix stripped, future item excluded
    past = call(svc, "news", "get_stock_news", {"symbol": "UP1"}, as_of=now - timedelta(days=3))
    assert past.data["items"] == []


def test_tool_specs_hide_injected_params(db):
    svc = make_service(db[1])
    specs = {s["name"]: s for s in svc.tool_specs(TOOL_ALLOWLIST["qa"])}
    ph = specs["get_price_history"]["input_schema"]
    assert "as_of" not in ph["properties"] and ph["required"] == ["symbol"]
    assert "place_paper_order" not in specs


def test_mcp_round_trip_in_process(db):
    _, f = db
    svc = make_service(f)

    async def go():
        async with McpToolGateway({"all": build_server(svc)}) as gw:
            specs = {s["name"]: s for s in gw.tool_specs(frozenset({"get_price_history", "score_horizon"}))}
            assert "caller_role" not in specs["get_price_history"]["input_schema"]["properties"]
            ok = await gw.call("technical", "get_price_history", {"symbol": "UP2"}, run_id=None, as_of=DECISION)
            bad = await gw.call("technical", "get_financials", {"symbol": "UP2"}, run_id=None, as_of=DECISION)
            return ok, bad
    ok, bad = asyncio.run(go())
    assert ok.ok and ok.meta.evidence_id and ok.data["symbol"] == "UP2"
    assert not bad.ok and bad.error.code == "FORBIDDEN"
    with f() as s:
        assert s.get(m.Evidence, ok.meta.evidence_id) is not None


def deps_for(f, cfg=None, record=True):
    store = SqlStore(f)
    cfg = cfg or Config()
    return Deps(cfg, AllowlistedTools(make_service(f)), store, ScriptedLLM(record_to=store if record else None),
                RecordingNotifier(), BudgetManager(cfg.budget_usd_per_run, cfg.budget_usd_per_day))


def test_on_demand_analysis_on_real_tools_and_replay():
    _, f = synthetic_db()
    deps = deps_for(f)
    out = asyncio.run(analyze(deps, "UP3", DECISION))
    res = {r["horizon"]: r for r in out["results"]}
    assert set(res) == {"short", "medium", "long"}
    short = res["short"]
    assert short["status"] in ("validated", "hitl_pending"), short["violations"]
    assert short["card"]["levels"]["stop"] < short["card"]["levels"]["entry_low"]
    assert "LLM-08" not in deps.llm.calls                       # no fundamentals -> no fundamental LLM call
    assert any("fundamental" in g for g in res["medium"]["gaps"])   # short never runs it
    with f() as s:
        run = s.get(m.Run, out["run_id"])
        assert run.trigger == "analyze:UP3:short,medium,long"
    # Replay: no tools or LLM are called live; levels and verdicts must match.
    rep = asyncio.run(replay_analysis(f, Config(), out["run_id"], notifier=RecordingNotifier()))
    assert rep["diffs"] == [] and rep["tool_misses"] == [] and rep["llm_misses"] == []


def test_resolve_by_company_name():
    _, f = synthetic_db(n_up=2, n_flat=0, n_down=0, days=120)
    out = asyncio.run(analyze(deps_for(f), "UP1 Industries", DECISION, ["short"]))
    assert out["results"][0]["symbol"] == "UP1"


def test_daily_run_on_real_tools_fails_closed_without_gate():
    _, f = synthetic_db(gate_passed=False)
    deps = deps_for(f)
    out = asyncio.run(run_daily(deps, DECISION, horizons=["short"]))
    assert out["status"] == "degraded" and not out.get("results")


def test_daily_run_on_real_tools():
    _, f = synthetic_db()
    cfg = Config(autonomy={f"publish_{h}": Autonomy.L2_APPROVE_EXCEPTIONS for h in ("short", "medium", "long")},
                 top_k_per_horizon=5)
    deps = deps_for(f, cfg)
    out = asyncio.run(run_daily(deps, DECISION, horizons=["short"]))
    assert out["status"] == "succeeded", out.get("errors")
    assert out["regime"]["regime"] in ("risk_on", "neutral", "risk_off")
    assert len(out["results"]) == 5 and all(r["symbol"].startswith("UP") for r in out["results"])
    assert count(f, m.ToolCall) > 20 and count(f, m.Evidence) > 20
