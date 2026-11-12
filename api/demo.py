"""Demo server on SYNTHETIC data (development only; never touches data/stock_primer.db).

    python -m api.demo            # builds data/demo.db if missing, then serves the API on :8000
    python -m api.demo --rebuild

Replays 20 trading days of the real pipeline on the synthetic market used by the
tests: daily runs (scripted LLM, autonomy L2), auto-follow fills, a manual
portfolio, outcome scoring and a digest. Prices, picks and P&L are made up.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "demo.db"
URL = f"sqlite:///{DB.as_posix()}"


def _deps(f, cfg):
    from core.settings import Settings
    from core.store import SqlStore
    from ingestion.adapters.gnews import GoogleNews  # noqa: F401  (news stays offline in the demo)
    from mcp_servers.registry import ToolContext
    from mcp_servers.service import ToolService
    from orchestrator.budget import BudgetManager
    from orchestrator.deps import Deps
    from orchestrator.fakes import ScriptedLLM
    from orchestrator.ports import AllowlistedTools
    from orchestrator.wiring import ConsoleNotifier
    from quant.engine import QuantEngine

    settings = Settings(universe="NIFTY200", database_url=URL)
    store = SqlStore(f)
    tools = ToolService(ToolContext(factory=f, quant=QuantEngine(f, settings), settings=settings, news=None), store)
    return Deps(cfg, AllowlistedTools(tools), store, ScriptedLLM(record_to=store), ConsoleNotifier(), BudgetManager(1e6, 1e6))


async def build() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from tests.synth import IST, synthetic_db, trading_days

    from core import models as m
    from evaluation.outcomes import score_outcomes
    from orchestrator.config import Autonomy, Config
    from orchestrator.digest import publish_digest
    from orchestrator.graphs import run_daily
    from hitl.service import HitlService
    from orchestrator.shadow import quant_shadow_picks
    from simulator.engine import Simulator

    DB.parent.mkdir(parents=True, exist_ok=True)
    for p in DB.parent.glob("demo.db*"):
        p.unlink()
    _, f = synthetic_db(url=URL)
    days = trading_days(21)
    with f() as s, s.begin():
        s.query(m.DataGateResult).delete()
        for d in days:
            s.add(m.DataGateResult(decision_date=d, passed=True, is_trading_day=True, issues=[], excluded=[], stats={}))
    cfg = Config(autonomy={f"publish_{h}": Autonomy.L2_APPROVE_EXCEPTIONS for h in ("short", "medium", "long")},
                 top_k_per_horizon=6, max_published_per_horizon=3, publish_confidence_min=0.55)
    deps = _deps(f, cfg)
    sim = Simulator(f)
    sim.ensure_standard_portfolios()
    manual = sim.create_portfolio("My picks", 500_000)
    for i, d in enumerate(days[:-1]):
        sim.run_eod(d)
        decision = datetime.combine(d, time(18, 30), IST)
        out = await run_daily(deps, decision, horizons=["short"], defer_digest=True)
        # Review: approve every other pending pick and reject the rest (feeds the human-review cohorts).
        hitl = HitlService(f)
        for k, t in enumerate(hitl.list_tasks()):
            if t["task_type"] == "approve_reco":
                hitl.decide(t["id"], "approve" if k % 2 == 0 else "reject", now=datetime.combine(d, time(19, 0), IST))
        sim.sync_auto_follow(datetime.combine(d, time(19, 30), IST))
        sim.sync_quant_shadow(decision, await quant_shadow_picks(deps, decision, "short", run_id=out.get("run_id")))
        if i == 3:
            with f() as s:
                r = s.scalar(select(m.Recommendation).where(m.Recommendation.status == "published"))
                sym = s.get(m.Instrument, r.instrument_id).symbol if r else "UP0"
            sim.place_order(manual, sym, amount=60_000, stop=float(r.stop) if r else None,
                            target=float(r.target) if r else None, horizon="short", reco_id=r.id if r else None,
                            idempotency_key="demo-manual-1", placed_at=datetime.combine(d, time(20), IST))
        print(f"  {d}: daily run done")
    last = days[-1]
    _demo_context(f, last, IST)
    sim.run_eod(last)
    score_outcomes(f, last)
    await run_daily(deps, datetime.combine(last, time(18, 30), IST), horizons=["short"], defer_digest=True)
    await publish_digest(deps, f, last)
    from orchestrator.premarket import publish_premarket

    await publish_premarket(deps, f, last)
    print(f"demo database ready at {DB}")


def _demo_context(f, last, IST) -> None:
    """Synthetic macro series, flows, events and results dates (made up, like the rest of the demo)."""
    import random
    from datetime import timedelta
    from decimal import Decimal

    from core import models as m
    from core.store import SqlStore

    rnd = random.Random(11)
    with f() as s, s.begin():
        base = {"BRENT": 82.0, "USDINR": 83.4, "US10Y": 4.2, "GOLD": 2650.0, "DXY": 103.0}
        for sid, v in base.items():
            for i in range(420):                      # long enough for the stress test's weekly regressions
                d = last - timedelta(days=419 - i)
                v *= 1 + rnd.gauss(0.0004 if sid == "BRENT" else 0.0001, 0.01 if sid in ("BRENT", "GOLD") else 0.002)
                s.merge(m.MacroSeries(series_id=sid, obs_date=d, value=Decimal(f"{v:.4f}"), source="demo"))
        for i in range(10):
            d = last - timedelta(days=9 - i)
            if d.weekday() < 5:
                s.merge(m.FlowsDaily(trade_date=d, fii_net=Decimal(str(rnd.randint(-4000, 1500))),
                                     dii_net=Decimal(str(rnd.randint(500, 4000))), source="demo"))
        events = [
            ("commodity-global-brent-spike", "commodity", "Brent rises on supply concerns", 4,
             [("Automobile and Auto Components", -1, 0.4), ("Chemicals", -1, 0.4), ("Information Technology", 1, 0.2)]),
            ("monetary-india-rbi-hold", "monetary_policy", "RBI holds repo rate, signals liquidity support", 3,
             [("Financial Services", 1, 0.3), ("Realty", 1, 0.3)]),
            ("trade-us-tariff-threat", "trade_policy", "US floats new tariffs on services imports", 3,
             [("Information Technology", -1, 0.35)]),
        ]
        for i, (key, typ, title, sev, impacts) in enumerate(events):
            e = m.Event(event_key=key, event_type=typ, title=title, summary=f"(demo) {title}.", severity=sev,
                        status="active", confidence=Decimal("0.7"), geographies=["GLOBAL" if i != 1 else "IN"],
                        first_seen=datetime.combine(last - timedelta(days=i), time(7, 15), IST), source_news_ids=[1, 2])
            s.add(e)
            s.flush()
            for sector, direction, mag in impacts:
                s.add(m.EventImpact(event_id=e.id, sector_id=SqlStore.sector_id(s, sector), direction=direction,
                                    magnitude=Decimal(str(mag)), horizon="short", channel=typ,
                                    details={"basis": "approved_map", "confidence": 0.7, "decay_days": 10}))
        for sym, days_ahead in (("UP2", 3), ("UP6", 6), ("FLAT1", 9), ("DN3", 12)):
            iid = SqlStore.instrument_id(s, sym)
            s.add(m.CorporateAction(instrument_id=iid, action_type="results", ex_date=last + timedelta(days=days_ahead),
                                    details={"subject": "Financial Results"}, source="demo"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    if args.rebuild or not DB.exists():
        asyncio.run(build())

    import uvicorn

    from core.db import make_engine, session_factory
    from orchestrator.config import Config

    from .main import create_app

    f = session_factory(make_engine(URL))

    @asynccontextmanager
    async def deps_cm():
        from orchestrator.wiring import load_config

        yield _deps(f, load_config(f) if False else Config())

    uvicorn.run(create_app(deps_cm, f), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
