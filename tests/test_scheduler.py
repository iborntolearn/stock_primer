"""A full simulated trading day through the scheduler jobs:
18:00 evening pipeline -> review -> 19:30 digest + auto-follow -> next day 23:00 evaluation."""
import asyncio
from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from core import models as m
from core.settings import Settings
from core.store import SqlStore
from hitl.service import HitlService
from ingestion.adapters.base import FetchResult, utcnow
from orchestrator.budget import BudgetManager
from orchestrator.config import Config
from orchestrator.deps import Deps
from orchestrator.fakes import RecordingNotifier, ScriptedLLM
from orchestrator.ports import AllowlistedTools
from scheduler import jobs
from simulator.engine import Simulator

from .synth import AS_OF, IST, synthetic_db
from .test_tools import make_service

NEXT = AS_OF + timedelta(days=1)   # Tuesday


class FakeNse:
    """Re-serves what is already in the database for a day (the synthetic world's 'bhavcopy')."""
    name = "nse_bhavcopy"

    def __init__(self, f):
        self.f = f

    def equity(self, day):
        with self.f() as s:
            rows = s.execute(select(m.PriceDaily, m.Instrument.symbol).join(m.Instrument)
                             .where(m.PriceDaily.trade_date == day)).all()
        return FetchResult([{"symbol": sym, "trade_date": day, "open": p.open, "high": p.high, "low": p.low,
                             "close": p.close, "prev_close": p.prev_close, "volume": p.volume,
                             "traded_value": p.traded_value, "delivery_pct": p.delivery_pct} for p, sym in rows],
                           self.name, utcnow())

    def indices(self, day):
        return FetchResult([], "nse_index_bhavcopy", utcnow())


class Empty:
    name = "x"

    def fetch(self, *a):
        return FetchResult([], "nse_corp_actions", utcnow())

    def daily(self, *a):
        return FetchResult([], "yfinance", utcnow())


def runtime(f, clock):
    store = SqlStore(f)
    cfg = Config(top_k_per_horizon=5)
    deps = Deps(cfg, AllowlistedTools(make_service(f)), store, ScriptedLLM(record_to=store), RecordingNotifier(),
                BudgetManager(5, 5))
    return jobs.Runtime(deps=deps, f=f, settings=Settings(universe="NIFTY200", reconcile_sample_size=0),
                        adapters=lambda: (FakeNse(f), Empty(), Empty()), now=lambda: clock[0])


def test_full_day_cycle():
    _, f = synthetic_db()
    with f() as s, s.begin():
        s.query(m.DataGateResult).delete()          # the pipeline must run the gate itself
    sim = Simulator(f)
    auto = sim.create_portfolio("Auto-follow", 1_000_000, type="auto_follow")
    clock = [datetime.combine(AS_OF, time(18, 0), IST)]
    rt = runtime(f, clock)

    out = asyncio.run(jobs.evening_pipeline(rt, AS_OF))
    assert out["gate_passed"] and out["run_status"] == "succeeded", out
    assert out["pending"] > 0 and out["published"] == 0              # autonomy L1: everything is reviewed
    assert out["shadow_orders"] == 5                                   # quant-only control takes the screen's top 5
    sent = rt.deps.notifier.sent
    assert sum(1 for x in sent if x["kind"] == "hitl_task") == out["tasks_notified"] == out["pending"]
    assert asyncio.run(jobs.evening_pipeline(rt, AS_OF)) == {"skipped": "daily run already done"}

    clock[0] = datetime.combine(AS_OF, time(19, 15), IST)
    assert asyncio.run(jobs.run_overdue_job(rt)) is False

    tasks = HitlService(f).list_tasks()
    HitlService(f).decide(tasks[0]["id"], "approve", now=datetime.combine(AS_OF, time(19, 0), IST))

    clock[0] = datetime.combine(AS_OF, time(19, 30), IST)
    d = asyncio.run(jobs.digest_job(rt, AS_OF))
    assert d["picks"] == 1 and d["auto_follow_orders"] == 1
    assert d["expired"] == len(tasks) - 1                             # undecided picks expire unpublished
    digest_text = rt.deps.notifier.sent[-1]["text"]
    assert "Daily digest" in digest_text and "Decision-support only" in digest_text

    # Next trading day: prices arrive, the 23:00 evaluation fills the auto-follow order at the open.
    with f() as s, s.begin():
        for p in s.scalars(select(m.PriceDaily).where(m.PriceDaily.trade_date == AS_OF)).all():
            s.add(m.PriceDaily(instrument_id=p.instrument_id, trade_date=NEXT, open=p.close, high=p.close * Decimal("1.01"),
                               low=p.close * Decimal("0.995"), close=p.close, adj_close=p.close, prev_close=p.close,
                               volume=p.volume, traded_value=p.traded_value, source="nse_bhavcopy"))
    clock[0] = datetime.combine(NEXT, time(23, 0), IST)
    ev = asyncio.run(jobs.evaluation_job(rt, NEXT))
    # 5 shadow orders, but all five picks are IT stocks: the 30% sector cap fills four and rejects the fifth.
    assert ev["fills"] == 1 + 4 and ev["activated"] == 1
    with f() as s:
        t = s.scalar(select(m.PaperTrade).where(m.PaperTrade.portfolio_id == auto))
        assert t.fill_date == NEXT and t.reason == "auto_follow"
        assert s.scalar(select(func.count()).select_from(m.DailySnapshot).where(m.DailySnapshot.portfolio_id == auto)) == 1
        shadow = s.scalar(select(m.Portfolio.id).where(m.Portfolio.type == "quant_shadow"))
        assert s.scalar(select(func.count()).select_from(m.PaperTrade).where(m.PaperTrade.portfolio_id == shadow)) == 4
        rejected = s.scalar(select(m.PaperOrder).where(m.PaperOrder.portfolio_id == shadow, m.PaperOrder.status == "rejected"))
        assert "sector" in rejected.reject_reason


def test_gate_failure_alerts_and_publishes_nothing():
    _, f = synthetic_db()
    with f() as s, s.begin():
        s.query(m.DataGateResult).delete()
        s.query(m.PriceDaily).filter(m.PriceDaily.trade_date == AS_OF).delete()   # no data for the day
    clock = [datetime.combine(AS_OF, time(18, 0), IST)]
    rt = runtime(f, clock)
    rt.sleep = lambda s: asyncio.sleep(0)
    out = asyncio.run(jobs.evening_pipeline(rt, AS_OF, max_attempts=2))
    assert out["gate_passed"] is False and out["published"] == 0 and out["run_status"] == "degraded"
    assert any("Data quality gate failed" in x["text"] for x in rt.deps.notifier.sent)
