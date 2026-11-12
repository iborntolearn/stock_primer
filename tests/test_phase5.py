"""Phase 5: what-if parsing, factor regressions and stress tests, historical replay, comparison,
CSV export, intraday stop-proximity alerts, and config switches that must not undo approved changes."""
import asyncio
import csv
import io
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from core import models as m
from core.settings import Settings
from evaluation.export import nav_csv, recommendations_csv, trades_csv
from hitl.service import HitlService
from ingestion.adapters.intraday import parse_intraday
from orchestrator.wiring import load_config
from scheduler import jobs
from simulator.alerts import Watch, check, evaluate
from simulator.stress import Shock, StressEngine, ols, parse_scenario, weekly

from .synth import AS_OF, IST, synthetic_db, trading_days
from .test_phase4 import deps_for

LATEST = {"BRENT": 88.0, "USDINR": 84.0, "US10Y": 4.2, "GOLD": 2400.0, "market": 24000.0}


# -- free-text scenarios -------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("What if crude hits $110?", {"BRENT": 25.0}),
    ("rupee falls 3% and US yields rise 50bp", {"USDINR": 3.0, "US10Y": 0.5}),
    ("rupee strengthens 2%", {"USDINR": -2.0}),
    ("nifty falls 10%", {"market": -10.0}),
    ("crude -20%", {"BRENT": -20.0}),
    ("US 10y to 4.8", {"US10Y": 0.6}),
    ("crude oil prices crash 30% with nifty down 5%", {"BRENT": -30.0, "market": -5.0}),
])
def test_parse_scenario(text, expected):
    shocks, _ = parse_scenario(text, LATEST)
    assert {x.factor: round(x.change, 4) for x in shocks} == expected


def test_parse_scenario_rejects_nonsense():
    assert parse_scenario("what if inflation rises", LATEST)[0] == []
    shocks, notes = parse_scenario("crude up 500%", LATEST)
    assert shocks == [] and "out of range" in notes[0]
    with pytest.raises(ValueError):
        Shock("VIX", 10)


# -- statistics ------------------------------------------------------------------------------------
def test_ols_recovers_coefficients_and_weekly_changes():
    rng = np.random.default_rng(1)
    X = pd.DataFrame({"a": rng.normal(0, 1, 200), "b": rng.normal(0, 1, 200)})
    y = 0.5 + 2.0 * X["a"] - 0.7 * X["b"] + rng.normal(0, 0.1, 200)
    fit = ols(y, X)
    assert abs(fit.coef["a"] - 2.0) < 0.05 and abs(fit.coef["b"] + 0.7) < 0.05 and fit.t["a"] > 50 and fit.r2 > 0.99
    assert ols(y.iloc[:10], X.iloc[:10]) is None                        # too few weeks
    lvl = pd.Series([100.0, 110.0], index=[date(2026, 10, 2), date(2026, 10, 9)])
    assert weekly(lvl).iloc[-1] == pytest.approx(10.0)
    assert weekly(pd.Series([4.0, 4.5], index=lvl.index), "pp").iloc[-1] == pytest.approx(0.5)


# -- stress engine on controlled data -----------------------------------------------------------------
def controlled_db():
    """NIFTY IT = 1.2 x market + 0.3 x USD/INR (daily % moves), so the answers are known."""
    _, f = synthetic_db(n_up=3, n_flat=0, n_down=3, days=300)
    rng = np.random.default_rng(3)
    days = trading_days(300)
    with f() as s, s.begin():
        mkt = [float(x) for x in s.scalars(select(m.IndexPriceDaily.close).where(m.IndexPriceDaily.index_code == "NIFTY 500")
                                           .order_by(m.IndexPriceDaily.trade_date))]
        mret = np.diff(np.log(mkt), prepend=np.log(mkt[0]))
        usd = rng.normal(0, 0.004, len(days))
        it = 20000 * np.exp(np.cumsum(1.2 * mret + 0.3 * usd + rng.normal(0, 0.001, len(days))))
        s.query(m.IndexPriceDaily).filter(m.IndexPriceDaily.index_code == "NIFTY IT").delete()
        for d, v, u in zip(days, it, 84 * np.exp(np.cumsum(usd))):
            s.add(m.IndexPriceDaily(index_code="NIFTY IT", trade_date=d, close=Decimal(str(round(v, 2)))))
            s.add(m.MacroSeries(series_id="USDINR", obs_date=d, value=Decimal(str(round(u, 4))), source="test"))
        p = m.Portfolio(name="Test", starting_cash=Decimal(1_000_000), cash=Decimal(500_000), type="manual")
        s.add(p)
        s.flush()
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == "UP0"))
        last = s.scalar(select(m.PriceDaily.close).where(m.PriceDaily.instrument_id == iid, m.PriceDaily.trade_date == AS_OF))
        s.add(m.Position(portfolio_id=p.id, instrument_id=iid, opened_on=AS_OF - timedelta(days=30), qty=100,
                         avg_cost=last, stop=last * Decimal("0.99"), target=last * Decimal("1.2"), horizon="short"))
        return f, p.id, float(last)


def test_factor_shock_sizes_sectors_and_checks_the_map():
    f, _, _ = controlled_db()
    with f() as s:
        r = StressEngine(s).run([Shock("USDINR", 4.0)])
    assert r.market["move_pct"] == 0.0 or abs(r.market["move_pct"]) < 0.5     # no real market link in the data
    it = next(x for x in r.sectors if x["sector"] == "Information Technology")
    usd = next(p for p in it["parts"] if p["factor"] == "USDINR")
    assert usd["used"] and abs(usd["per_unit"] - 0.3) < 0.08
    assert 0.8 < it["move_pct"] < 1.6
    (chk,) = it["map"]                                     # seed map: weaker rupee helps IT
    assert chk["map_direction"] == 1 and chk["agreement"] == "agrees"


def test_market_shock_flags_stop_breaches_and_sizes_pnl():
    f, pid, last = controlled_db()
    with f() as s:
        r = StressEngine(s).run([Shock("market", -10.0)], [pid])
    it = next(x for x in r.sectors if x["sector"] == "Information Technology")
    assert -13.5 < it["move_pct"] < -10.5                  # beta 1.2
    (pf,) = r.portfolios
    (h,) = pf["holdings"]
    assert h["symbol"] == "UP0" and h["move_pct"] < 0 and h["breaches_stop"] is True
    assert h["pnl"] == pytest.approx(100 * last * h["move_pct"] / 100, rel=1e-3)
    assert pf["nav"] == pytest.approx(500_000 + 100 * last) and pf["stops_breached"] == 1
    assert any("not a forecast" in a for a in r.assumptions)


def test_large_shock_warns_about_extrapolation():
    f, _, _ = controlled_db()
    with f() as s:
        r = StressEngine(s).run([Shock("USDINR", 20.0)])
    assert any("largest weekly move" in n for n in r.notes)


def test_replay_uses_worst_window_and_actual_returns():
    f, pid, _ = controlled_db()
    with f() as s:
        e = StressEngine(s)
        start, end, bret = e.worst_window(20)
        r = e.replay(20, portfolio_ids=[pid])
    assert r["window"]["start"] == start.isoformat() and r["window"]["benchmark_pct"] == round(bret, 2) < 0
    (h,) = r["portfolios"][0]["holdings"]
    assert h["basis"] == "actual"
    with f() as s:
        mine = StressEngine(s).replay(start=start, end=end, portfolio_ids=[pid])
    assert mine["window"]["benchmark_pct"] == r["window"]["benchmark_pct"]


# -- CSV export ------------------------------------------------------------------------------------------
def test_csv_exports():
    f, pid, last = controlled_db()
    with f() as s, s.begin():
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == "UP0"))
        s.add(m.PaperTrade(portfolio_id=pid, instrument_id=iid, side="BUY", qty=100, fill_price=Decimal("500.5"),
                           fill_date=AS_OF - timedelta(days=30), total_cost=Decimal("61.2"), reason="manual"))
        s.add(m.DailySnapshot(portfolio_id=pid, snap_date=AS_OF, cash=Decimal(500000), holdings_value=Decimal(50050),
                              nav=Decimal(550050), benchmark_nav=Decimal(548000)))
    with f() as s:
        t, n, r = trades_csv(s, pid), nav_csv(s, pid), recommendations_csv(s)
    assert t.startswith("# stock_primer export")
    rows = list(csv.DictReader(io.StringIO(t.split("\n", 1)[1])))
    assert rows[0]["symbol"] == "UP0" and rows[0]["value"] == "50050.0" and rows[0]["stt"] == ""
    assert list(csv.DictReader(io.StringIO(n.split("\n", 1)[1])))[0]["nav"] == "550050.0"
    assert r.split("\n")[1].startswith("reco_id,")


# -- intraday alerts -----------------------------------------------------------------------------------------
def test_parse_intraday_uses_latest_session():
    t0 = int(datetime(2026, 10, 13, 9, 15, tzinfo=IST).timestamp())
    payload = {"chart": {"result": [{"timestamp": [t0 - 86400, t0, t0 + 300, t0 + 600],
                                     "indicators": {"quote": [{"close": [90, 100, 97, None], "high": [91, 101, 98, None],
                                                               "low": [89, 99, 96, None]}]}}]}}
    q = parse_intraday("ABC", payload)
    assert (q.last, q.day_high, q.day_low) == (97, 101, 96) and q.at.hour == 9


def test_evaluate_alert_kinds():
    w = Watch("pos:1:ABC", "ABC", "x", stop=100.0, target=120.0)
    assert evaluate(w, 101.0, 102.0, 100.5, 1.5) == [("near_stop", 101.0, 100.0)]
    assert evaluate(w, 101.0, 102.0, 99.0, 1.5) == [("stop_breach", 99.0, 100.0)]
    assert evaluate(w, 110.0, 121.0, 105.0, 1.5) == [("target_hit", 121.0, 120.0)]
    assert evaluate(w, 110.0, 111.0, 105.0, 1.5) == []


def quote(last, hi, lo):
    return SimpleNamespace(last=last, day_high=hi, day_low=lo, at=datetime(2026, 10, 13, 11, 0, tzinfo=IST))


def test_alerts_fire_once_per_day_and_breach_supersedes_near():
    f, _, last = controlled_db()
    day = AS_OF + timedelta(days=1)
    near = check(f, {"UP0": quote(last * 0.995, last, last * 0.993)}, day, 1.5)
    assert [a["kind"] for a in near] == ["near_stop"] and "delayed" in near[0]["text"]
    assert check(f, {"UP0": quote(last * 0.995, last, last * 0.993)}, day, 1.5) == []
    breach = check(f, {"UP0": quote(last * 0.985, last, last * 0.98)}, day, 1.5)
    assert [a["kind"] for a in breach] == ["stop_breach"]
    assert check(f, {"UP0": quote(last * 0.995, last, last * 0.98)}, day, 1.5) == []
    assert [a["kind"] for a in check(f, {"UP0": quote(last * 0.995, last, last * 0.993)}, day + timedelta(days=1), 1.5)] == ["near_stop"]


class FakeQuotes:
    def __init__(self, quotes):
        self.q, self.calls = quotes, 0

    def quotes(self, symbols):
        self.calls += 1
        return {s: self.q[s] for s in symbols if s in self.q}, []


def test_intraday_job_respects_switch_and_market_hours():
    f, _, last = controlled_db()
    tue = AS_OF + timedelta(days=1)
    clock = [datetime.combine(tue, time(11, 0), IST)]
    rt = jobs.Runtime(deps=deps_for(f), f=f, settings=Settings(universe="NIFTY200"), adapters=lambda: None, now=lambda: clock[0])
    rt.quote_source = FakeQuotes({"UP0": quote(last * 0.97, last, last * 0.96)})
    assert asyncio.run(jobs.intraday_alerts_job(rt)) == {"skipped": "intraday alerts are off"}
    rt.deps.cfg.intraday_alerts = True
    clock[0] = datetime.combine(tue, time(16, 0), IST)
    assert asyncio.run(jobs.intraday_alerts_job(rt)) == {"skipped": "market closed"}
    clock[0] = datetime.combine(tue, time(11, 0), IST)
    out = asyncio.run(jobs.intraday_alerts_job(rt))
    assert out["alerts"] == ["stop_breach:UP0"] and rt.quote_source.calls == 1
    assert rt.deps.notifier.sent[-1]["kind"] == "alert"


# -- API --------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def api():
    f, pid, _ = controlled_db()

    @asynccontextmanager
    async def deps_cm():
        yield deps_for(f)

    from api.main import create_app

    with TestClient(create_app(deps_cm, f)) as c:
        c.f, c.pid = f, pid
        yield c


def test_stress_api(api):
    pre = api.get("/api/v1/stress/presets").json()
    assert {p["id"] for p in pre["presets"]} >= {"crude_spike", "correction"} and pre["latest"]["USDINR"] > 0
    r = api.post("/api/v1/stress", json={"preset": "correction", "portfolio_ids": [api.pid]})
    assert r.status_code == 200 and r.json()["market"]["move_pct"] == -10.0 and r.json()["portfolios"][0]["portfolio_id"] == api.pid
    t = api.post("/api/v1/stress", json={"text": "what if the rupee falls 3%?"}).json()
    assert t["scenario"]["shocks"] == [{"factor": "USDINR", "change": 3.0}]
    bad = api.post("/api/v1/stress", json={"text": "what if it rains"})
    assert bad.status_code == 422 and bad.json()["code"] == "UNPARSED"
    assert api.post("/api/v1/stress", json={"shocks": [{"factor": "VIX", "change": 5}]}).status_code == 422
    rp = api.post("/api/v1/stress/replay", json={"days": 10})
    assert rp.status_code == 200 and rp.json()["window"]["trading_days"] == 10
    assert api.post("/api/v1/stress/replay", json={"start": "2026-09-01"}).status_code == 422


def test_compare_api(api):
    r = api.get("/api/v1/compare", params={"symbols": "UP0,DN0"})
    assert r.status_code == 200, r.text
    body = r.json()
    up, dn = body["items"]
    assert up["prices"]["ret_3m_pct"] > dn["prices"]["ret_3m_pct"]
    assert set(body["rebased"]) == {"UP0", "DN0", "NIFTY 500"} and body["rebased"]["UP0"][0]["value"] == 100.0
    assert up["scores"]["short"]["quant_score"] > dn["scores"]["short"]["quant_score"]
    assert api.get("/api/v1/compare", params={"symbols": "UP0"}).status_code == 422
    assert api.get("/api/v1/compare", params={"symbols": "UP0,NOPE"}).status_code == 404


def test_export_api(api):
    r = api.get(f"/api/v1/export/portfolios/{api.pid}/positions.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="test-positions.csv"' in r.headers["content-disposition"] and "UP0" in r.text
    assert api.get(f"/api/v1/export/portfolios/{api.pid}/secrets.csv").status_code == 404
    assert api.get("/api/v1/export/recommendations.csv").status_code == 200


def test_switches_keep_approved_changes(api):
    r = api.post("/api/v1/config/autonomy", json={"action": "publish_long", "level": 2}).json()
    HitlService(api.f).decide(r["task_id"], "approve", now=datetime.now(IST))
    assert api.post("/api/v1/admin/kill-switch", json={"enabled": True}).json() == {"kill_switch": True}
    a = api.post("/api/v1/config/alerts", json={"enabled": True, "stop_proximity_pct": 2.0}).json()
    assert a == {"intraday_alerts": True, "stop_proximity_pct": 2.0}
    cfg = load_config(api.f)
    assert cfg.autonomy["publish_long"] == 2 and cfg.kill_switch and cfg.intraday_alerts     # nothing undone
    api.post("/api/v1/admin/kill-switch", json={"enabled": False})
    assert api.get("/api/v1/alerts").json() == {"items": []}
