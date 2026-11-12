"""Review inbox decisions, fail-safe expiry, and the 19:30 digest."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from core import models as m
from core.store import SqlStore, default_due
from hitl.service import DecisionError, HitlService
from orchestrator.budget import BudgetManager
from orchestrator.config import Config
from orchestrator.deps import Deps
from orchestrator.digest import publish_digest
from orchestrator.fakes import FakeTools, RecordingNotifier, ScriptedLLM
from orchestrator.ports import AllowlistedTools

from .test_simulator import DAYS, World, _published_reco, evening

IST = timezone(timedelta(hours=5, minutes=30))


def pending_reco(w, **kw):
    rid = _published_reco(w, **kw)
    with w.f() as s, s.begin():
        r = s.get(m.Recommendation, rid)
        r.status, r.published_at = "hitl_pending", None
    tid = SqlStore(w.f).create_hitl_task("approve_reco", "recommendation", rid, {"card": {}}, "approve", "autonomy L1",
                                         priority=2, due_at=evening(DAYS[0]) + timedelta(minutes=25))
    return rid, tid


def reco(w, rid):
    with w.f() as s:
        return s.get(m.Recommendation, rid)


@pytest.fixture
def w():
    return World()


def test_default_due_dates():
    at_1830 = datetime(2026, 10, 12, 18, 30, tzinfo=IST)
    assert default_due("approve_reco", at_1830) == datetime(2026, 10, 12, 19, 25, tzinfo=IST)
    at_2000 = datetime(2026, 10, 12, 20, 0, tzinfo=IST)
    assert default_due("approve_reco", at_2000) == datetime(2026, 10, 13, 19, 25, tzinfo=IST)


def test_approve_publishes_and_records_decision(w):
    rid, tid = pending_reco(w)
    h = HitlService(w.f)
    res = h.decide(tid, "approve", note="looks fine")
    assert res.status == "approved" and reco(w, rid).status == "published" and reco(w, rid).published_at
    t = h.get(tid)
    assert t["decided_by"] == "owner" and t["decision"]["ai_recommendation"] == "approve" and t["decision_note"] == "looks fine"
    with pytest.raises(DecisionError, match="already approved"):
        h.decide(tid, "reject")


def test_reject_keeps_unpublished(w):
    rid, tid = pending_reco(w)
    HitlService(w.f).decide(tid, "reject")
    assert reco(w, rid).status == "rejected" and reco(w, rid).published_at is None


def test_edit_within_guard_ranges(w):
    rid, tid = pending_reco(w, stop=90, target=120)
    h = HitlService(w.f)
    with pytest.raises(DecisionError, match="more than 20%"):
        h.decide(tid, "edit", edits={"target": 160})
    with pytest.raises(DecisionError, match="below the short floor"):
        h.decide(tid, "edit", edits={"target": 105})
    with pytest.raises(DecisionError, match="only stop/target"):
        h.decide(tid, "edit", edits={"entry_low": 95})
    res = h.decide(tid, "edit", edits={"stop": 92})
    r = reco(w, rid)
    assert res.status == "edited" and float(r.stop) == 92 and r.card["levels"]["stop"] == 92 and r.status == "published"


def test_action_must_fit_task_type(w):
    _, tid = pending_reco(w)
    with pytest.raises(DecisionError, match="not allowed"):
        HitlService(w.f).decide(tid, "exclude")


def test_expiry_applies_safe_default(w):
    rid, tid = pending_reco(w)
    store = SqlStore(w.f)
    remind = store.create_hitl_task("review_thesis_break", "recommendation", rid, {}, "close", "news", 1,
                                    default_on_expiry="keep_and_remind", due_at=evening(DAYS[0]))
    h = HitlService(w.f)
    out = {r.task_id: r for r in h.expire_due(now=evening(DAYS[0]) + timedelta(hours=1))}
    assert out[tid].status == "expired" and out[tid].action == "reject"
    assert reco(w, rid).status == "rejected"
    assert out[remind].action == "remind" and h.get(remind)["status"] == "pending"
    assert h.expire_due(now=evening(DAYS[0]) + timedelta(hours=2)) == []


def test_thesis_break_close_invalidates_and_simulator_exits(w):
    pid = w.sim.create_portfolio("auto", 1_000_000, type="auto_follow")
    rid = _published_reco(w)
    w.sim.sync_auto_follow(evening(DAYS[0]))
    w.flat("AAA", DAYS[:3])
    w.sim.run_eod_portfolio(pid, DAYS[1])
    tid = SqlStore(w.f).create_hitl_task("review_thesis_break", "recommendation", rid, {}, "close", "event", 1)
    HitlService(w.f).decide(tid, "close")
    assert reco(w, rid).status == "invalidated"
    w.sim.run_eod_portfolio(pid, DAYS[2])
    assert w.trades(pid)[-1].reason == "thesis_break"


def test_adjust_stop_moves_open_positions(w):
    pid = w.sim.create_portfolio("auto", 1_000_000, type="auto_follow")
    rid = _published_reco(w)
    w.sim.sync_auto_follow(evening(DAYS[0]))
    w.flat("AAA", DAYS[:2])
    w.sim.run_eod_portfolio(pid, DAYS[1])
    tid = SqlStore(w.f).create_hitl_task("review_thesis_break", "recommendation", rid, {}, "adjust_stop", "x", 1)
    res = HitlService(w.f).decide(tid, "adjust_stop", edits={"stop": 95})
    assert "1 open position" in res.effects[0]
    with w.f() as s:
        assert float(s.scalar(select(m.Position.stop))) == 95


def test_digest_lists_published_and_pending(w):
    published = _published_reco(w, "AAA")
    _, pending = pending_reco(w, sym="BBB")
    store = SqlStore(w.f)
    run_id = store.create_run("daily", evening(DAYS[0]), "schedule", 1, "s", "p")
    store.save_agent_output(run_id, "run_summary", "run", str(run_id), {
        "horizons": ["short", "medium"], "data_ok": True, "no_pick_reasons": {"medium": "3 screened, none favourable"}},
        "passed", None)
    notifier = RecordingNotifier()
    cfg = Config()
    deps = Deps(cfg, AllowlistedTools(FakeTools(store)), store, ScriptedLLM(), notifier, BudgetManager(3, 6))
    out = asyncio.run(publish_digest(deps, w.f, DAYS[0]))
    text = notifier.sent[0]["text"]
    assert "AAA" in text and "Awaiting review: 1 approve reco" in text
    assert "medium: 3 screened" in text and "Decision-support only" in text
    assert out["digest"]["picks"][0]["symbol"] == "AAA"
