"""The orchestrator graphs run unchanged on the SQL store (SQLite in-memory here, Postgres later)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from core import models as m
from core.db import make_engine, session_factory
from core.store import SqlStore
from orchestrator.budget import BudgetManager
from orchestrator.config import Autonomy, Config
from orchestrator.deps import Deps
from orchestrator.fakes import FakeTools, RecordingNotifier, ScriptedLLM
from orchestrator.graphs import chat, run_daily, run_event_digest, run_event_triggered
from orchestrator.ports import AllowlistedTools

IST = timezone(timedelta(hours=5, minutes=30))
DT = datetime(2026, 10, 13, 18, 30, tzinfo=IST)


@pytest.fixture
def sql():
    engine = make_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    factory = session_factory(engine)
    cfg = Config(autonomy={f"publish_{h}": Autonomy.L2_APPROVE_EXCEPTIONS for h in ("short", "medium", "long")})
    store = SqlStore(factory)
    deps = Deps(cfg, AllowlistedTools(FakeTools(store)), store, ScriptedLLM(), RecordingNotifier(),
                BudgetManager(cfg.budget_usd_per_run, cfg.budget_usd_per_day))
    return deps, factory


def count(factory, model, *where):
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def test_daily_run_persists_everything(sql):
    deps, f = sql
    out = asyncio.run(run_daily(deps, DT, horizons=["short"]))
    assert out["status"] == "succeeded", out.get("errors")
    assert len(out["published"]) == 3
    assert count(f, m.Recommendation) == 5
    assert count(f, m.Recommendation, m.Recommendation.status == "published") == 3
    assert count(f, m.RecoEvidence) > 0
    assert count(f, m.RegimeSnapshot) == 1
    with f() as s:
        run = s.get(m.Run, out["run_id"])
        assert run.status == "succeeded" and run.finished_at is not None
        reco = s.scalars(select(m.Recommendation).where(m.Recommendation.status == "published")).first()
        assert reco.card["disclaimer_key"] == "decision_support_v1"
        assert reco.published_at.tzinfo is not None


def test_events_and_shadow_reevaluation(sql):
    deps, f = sql
    asyncio.run(run_daily(deps, DT, horizons=["short"]))
    triggers = asyncio.run(run_event_digest(deps, DT))["triggers"]
    assert triggers and count(f, m.EventImpact) == 2
    events = deps.store.active_events(DT - timedelta(days=1))
    assert {i["sector"] for i in events[0]["impacts"]} == {"Energy", "Auto"}

    deps.llm.hooks["LLM-07"] = lambda o, s: {**o, "score_adjustment": -1.0} if o["symbol"] == "ONGC" else o
    deps.llm.hooks["LLM-09"] = lambda o, s: {**o, "score_adjustment": -1.0} if o["symbol"] == "ONGC" else o
    out = asyncio.run(run_event_triggered(deps, DT + timedelta(hours=1), triggers))
    assert len(out["tasks"]) == 1
    assert count(f, m.HitlTask, m.HitlTask.task_type == "review_thesis_break") == 1


def test_duplicate_suppression_uses_db(sql):
    deps, f = sql
    asyncio.run(run_daily(deps, DT, horizons=["short"]))
    out = asyncio.run(run_daily(deps, DT + timedelta(days=1), horizons=["short"]))
    assert out["published"] == []  # same picks, same levels, inside the cooldown window


def test_qa_citations_resolve_to_stored_evidence(sql):
    deps, _ = sql
    out = asyncio.run(chat(deps, "How is the market?", DT))
    ref = out["citations"][0]
    assert deps.store.get_evidence(int(ref[1:])) is not None


def test_evidence_dedupes_identical_payloads(sql):
    deps, _ = sql
    a = deps.store.add_evidence("calc", "x", DT, {"a": 1}, None)
    b = deps.store.add_evidence("calc", "x", DT, {"a": 1}, None)
    c = deps.store.add_evidence("calc", "x", DT, {"a": 2}, None)
    assert a == b != c


def test_evidence_dedupe_respects_kind_and_source(sql):
    deps, _ = sql
    a = deps.store.add_evidence("news", "google_news_rss", DT, {"items": []}, None)
    b = deps.store.add_evidence("event", "nse_corp_actions", DT, {"items": []}, None)
    assert a != b and deps.store.get_evidence(b).kind == "event"
