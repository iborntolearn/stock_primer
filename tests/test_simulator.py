"""Simulator, outcome scoring and scorecard on hand-built price paths."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from core import models as m
from core.db import make_engine, session_factory
from ingestion.reference import seed, sync_calendar, upsert_instrument
from evaluation.outcomes import score_outcomes
from evaluation.scorecard import scorecard
from simulator.costs import trade_costs
from simulator.engine import OrderRejected, Simulator
from simulator.metrics import performance, xirr

IST = timezone(timedelta(hours=5, minutes=30))
D0 = date(2026, 10, 12)   # Monday


def days(n):
    out, d = [], D0
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = days(15)


def evening(d):
    return datetime.combine(d, time(19, 0), IST)


class World:
    """One or two instruments with fully specified daily bars."""

    def __init__(self):
        self.engine = make_engine("sqlite:///:memory:")
        m.Base.metadata.create_all(self.engine)
        self.f = session_factory(self.engine)
        with self.f() as s, s.begin():
            seed(s)
            sync_calendar(s, D0 - timedelta(days=40), D0 + timedelta(days=60), [])
            self.ids = {sym: upsert_instrument(s, sym, name=sym, industry=ind)
                        for sym, ind in (("AAA", "Information Technology"), ("BBB", "Information Technology"), ("CCC", "Power"))}
            # 20 quiet history days so ADV exists
            for sym in self.ids:
                d = D0 - timedelta(days=1)
                n = 0
                while n < 20:
                    if d.weekday() < 5:
                        self._bar(s, sym, d, 100, 101, 99, 100, 1_000_000)
                        n += 1
                    d -= timedelta(days=1)
            for i, d in enumerate([D0 - timedelta(days=30)] + DAYS):
                s.merge(m.IndexPriceDaily(index_code="NIFTY 500", trade_date=d, close=Decimal(20000 + 10 * i)))
        self.sim = Simulator(self.f)

    def _bar(self, s, sym, d, o, h, l, c, v=1_000_000):
        s.merge(m.PriceDaily(instrument_id=self.ids[sym], trade_date=d, open=Decimal(str(o)), high=Decimal(str(h)),
                             low=Decimal(str(l)), close=Decimal(str(c)), adj_close=Decimal(str(c)), prev_close=Decimal(str(c)),
                             volume=v, traded_value=Decimal(str(c * v)), source="test"))

    def bar(self, sym, d, o, h, l, c, v=1_000_000):
        with self.f() as s, s.begin():
            self._bar(s, sym, d, o, h, l, c, v)

    def flat(self, sym, ds, px=100):
        for d in ds:
            self.bar(sym, d, px, px + 1, px - 1, px)

    def trades(self, pid):
        with self.f() as s:
            return s.scalars(select(m.PaperTrade).where(m.PaperTrade.portfolio_id == pid).order_by(m.PaperTrade.id)).all()

    def portfolio(self, pid):
        with self.f() as s:
            return s.get(m.Portfolio, pid)


@pytest.fixture
def w():
    return World()


def test_costs_buy_vs_sell():
    r = {"brokerage_pct": 0.0, "brokerage_max_per_order": 20, "stt_delivery_pct": 0.1, "exchange_txn_pct": 0.00297,
         "sebi_fee_per_crore": 10, "gst_pct": 18, "stamp_duty_buy_pct": 0.015, "dp_charge_per_sell_scrip": 15.93}
    buy, sell = trade_costs("BUY", 100, 1000, r), trade_costs("SELL", 100, 1000, r)
    assert buy["stt"] == sell["stt"] == 100.0
    assert buy["stamp_duty"] == 15.0 and sell["stamp_duty"] == 0
    assert sell["dp_charges"] == 15.93 and buy["dp_charges"] == 0


def test_fill_at_next_open_never_signal_bar(w):
    pid = w.sim.create_portfolio("p", 1_000_000)
    o = w.sim.place_order(pid, "AAA", amount=50_000, stop=90, target=130, horizon="short", placed_at=evening(DAYS[0]))
    assert o["expected_fill"] == f"{DAYS[1].isoformat()} open"
    w.flat("AAA", DAYS[:2])
    w.bar("AAA", DAYS[1], 102, 104, 101, 103)
    assert w.sim.run_eod_portfolio(pid, DAYS[0]).filled == []
    rep = w.sim.run_eod_portfolio(pid, DAYS[1])
    assert rep.filled == ["AAA"]
    (t,) = w.trades(pid)
    assert float(t.fill_price) > 102 and t.fill_date == DAYS[1]            # open plus slippage
    assert t.qty == int(50_000 // float(t.fill_price))
    p = w.portfolio(pid)
    assert float(p.cash) == pytest.approx(1_000_000 - t.qty * float(t.fill_price) - float(t.total_cost), abs=0.01)
    assert rep.nav == pytest.approx(float(p.cash) + t.qty * 103, abs=0.01)


def test_idempotent_order_and_eod(w):
    pid = w.sim.create_portfolio("p", 100_000)
    a = w.sim.place_order(pid, "AAA", qty=10, idempotency_key="k1", placed_at=evening(DAYS[0]))
    b = w.sim.place_order(pid, "AAA", qty=10, idempotency_key="k1", placed_at=evening(DAYS[0]))
    assert a["order_id"] == b["order_id"]
    w.flat("AAA", DAYS[:2])
    w.sim.run_eod_portfolio(pid, DAYS[1])
    assert w.sim.run_eod_portfolio(pid, DAYS[1]).skipped
    assert len(w.trades(pid)) == 1


@pytest.mark.parametrize("bar,expect_reason,expect_price", [
    ((99, 100, 89, 95), "stop", 90.0),           # intraday stop
    ((85, 88, 84, 86), "stop", 85.0),            # gap through stop fills at the (worse) open
    ((135, 136, 133, 134), "target", 135.0),     # gap through target fills at the (better) open
    ((101, 131, 100, 128), "target", 130.0),     # intraday target
])
def test_exits(w, bar, expect_reason, expect_price):
    pid = w.sim.create_portfolio("p", 1_000_000)
    w.sim.place_order(pid, "AAA", qty=100, stop=90, target=130, horizon="short", placed_at=evening(DAYS[0]))
    w.flat("AAA", DAYS[:2])
    w.sim.run_eod_portfolio(pid, DAYS[1])
    w.bar("AAA", DAYS[2], *bar)
    rep = w.sim.run_eod_portfolio(pid, DAYS[2])
    sell = w.trades(pid)[-1]
    assert sell.side == "SELL" and sell.reason == expect_reason
    assert float(sell.fill_price) == pytest.approx(expect_price * (1 - 7e-4), rel=2e-3)   # minus small slippage


def test_reject_when_open_gaps_below_stop(w):
    pid = w.sim.create_portfolio("p", 100_000)
    w.sim.place_order(pid, "AAA", qty=10, stop=95, placed_at=evening(DAYS[0]))
    w.bar("AAA", DAYS[1], 94, 96, 93, 95)
    rep = w.sim.run_eod_portfolio(pid, DAYS[1])
    assert rep.filled == [] and "below stop" in rep.rejected[0]


def test_caps_and_cash(w):
    pid = w.sim.create_portfolio("p", 10_000_000, rules={"adv_cap_pct": 1.0, "max_position_pct": 100, "max_sector_pct": 100})
    w.sim.place_order(pid, "AAA", amount=5_000_000, placed_at=evening(DAYS[0]))
    w.flat("AAA", DAYS[:2])
    w.sim.run_eod_portfolio(pid, DAYS[1])
    assert w.trades(pid)[0].qty == 10_000                                   # 1% of ADV volume 1,000,000
    small = w.sim.create_portfolio("small", 1_000, rules={"max_position_pct": 100})
    w.sim.place_order(small, "AAA", qty=50, placed_at=evening(DAYS[0]))
    rep = w.sim.run_eod_portfolio(small, DAYS[1])
    assert w.trades(small)[0].qty < 10                                       # scaled down to fit cash incl. costs
    assert float(w.portfolio(small).cash) >= 0


def test_sector_cap(w):
    pid = w.sim.create_portfolio("p", 1_000_000, rules={"max_position_pct": 25, "max_sector_pct": 30})
    w.sim.place_order(pid, "AAA", amount=250_000, placed_at=evening(DAYS[0]))
    w.sim.place_order(pid, "BBB", amount=250_000, placed_at=evening(DAYS[0]))
    for sym in ("AAA", "BBB"):
        w.flat(sym, DAYS[:2])
    w.sim.run_eod_portfolio(pid, DAYS[1])
    a, b = w.trades(pid)
    assert a.qty * float(a.fill_price) + b.qty * float(b.fill_price) <= 300_000 + 1   # both are IT


def test_circuit_lock_retries_then_expires(w):
    pid = w.sim.create_portfolio("p", 100_000)
    w.sim.place_order(pid, "CCC", qty=10, placed_at=evening(DAYS[0]))
    for d in DAYS[1:4]:
        w.bar("CCC", d, 120, 120, 120, 120, v=0)
        w.sim.run_eod_portfolio(pid, d)
    with w.f() as s:
        o = s.scalar(select(m.PaperOrder))
        assert o.status == "expired" and o.attempts == 3


def test_split_and_dividend(w):
    pid = w.sim.create_portfolio("p", 1_000_000)
    w.sim.place_order(pid, "AAA", qty=100, stop=90, target=200, placed_at=evening(DAYS[0]))
    w.flat("AAA", DAYS[:2])
    w.sim.run_eod_portfolio(pid, DAYS[1])
    with w.f() as s, s.begin():
        s.add(m.CorporateAction(instrument_id=w.ids["AAA"], action_type="split", ex_date=DAYS[2],
                                ratio_num=Decimal(10), ratio_den=Decimal(5), source="t"))
        s.add(m.CorporateAction(instrument_id=w.ids["AAA"], action_type="dividend", ex_date=DAYS[3],
                                amount_per_share=Decimal("2"), source="t"))
    w.bar("AAA", DAYS[2], 50, 51, 49.5, 50)
    cash_before = float(w.portfolio(pid).cash)
    w.sim.run_eod_portfolio(pid, DAYS[2])
    with w.f() as s:
        pos = s.scalar(select(m.Position).where(m.Position.portfolio_id == pid))
        assert pos.qty == 200 and float(pos.stop) == 45 and pos.status == "open"   # stop not hit after the split
    w.bar("AAA", DAYS[3], 50, 51, 49.5, 50)
    w.sim.run_eod_portfolio(pid, DAYS[3])
    assert float(w.portfolio(pid).cash) == pytest.approx(cash_before + 400, abs=0.01)


def test_horizon_expiry(w):
    pid = w.sim.create_portfolio("p", 100_000, rules={"holding_days": {"short": 3}})
    w.sim.place_order(pid, "AAA", qty=10, horizon="short", placed_at=evening(DAYS[0]))
    w.flat("AAA", DAYS[:6])
    for d in DAYS[1:6]:
        w.sim.run_eod_portfolio(pid, d)
    assert w.trades(pid)[-1].reason == "horizon_end"


def _published_reco(w, sym="AAA", stop=90, target=120, pub_day=DAYS[0], horizon="short", confidence=0.62):
    with w.f() as s, s.begin():
        run = m.Run(run_type="daily", decision_time=evening(pub_day), status="succeeded")
        s.add(run)
        s.flush()
        r = m.Recommendation(run_id=run.id, instrument_id=w.ids[sym], horizon=horizon, verdict="favourable",
                             action="accumulate", quant_score=80, final_score=82, confidence=Decimal(str(confidence)),
                             entry_low=99, entry_high=101, stop=stop, target=target, thesis="t",
                             card={"symbol": sym, "horizon": horizon, "headline": f"{sym} breakout",
                                   "levels": {"entry_low": 99, "entry_high": 101, "stop": stop, "target": target}},
                             status="published", published_at=evening(pub_day), meta={"run_type": "daily"})
        s.add(r)
        s.flush()
        return r.id


def test_auto_follow_orders_every_published_pick_once(w):
    pid = w.sim.create_portfolio("auto", 1_000_000, type="auto_follow")
    rid = _published_reco(w)
    a = w.sim.sync_auto_follow(evening(DAYS[0]))
    b = w.sim.sync_auto_follow(evening(DAYS[0]))
    assert len(a) == 1 and a[0]["reco_id"] == rid and len(b) == 0
    assert a[0]["amount"] == pytest.approx(100_000, rel=0.01)       # 1/10th of NAV
    w.flat("AAA", DAYS[:2])
    w.sim.run_eod_portfolio(pid, DAYS[1])
    t = w.trades(pid)[0]
    assert t.reco_id == rid and t.reason == "auto_follow"


def test_outcome_scoring_target_and_active(w):
    hit = _published_reco(w, "AAA", stop=90, target=110)
    open_ = _published_reco(w, "BBB", stop=90, target=150)
    w.flat("AAA", DAYS[:2])
    w.flat("BBB", DAYS[:4])
    w.bar("AAA", DAYS[2], 101, 112, 100, 111)
    rep = score_outcomes(w.f, DAYS[3])
    assert any(x.startswith("AAA/short target") for x in rep.scored) and rep.activated == ["BBB"]
    with w.f() as s:
        o = s.get(m.Outcome, hit)
        assert o.hit_target and float(o.ret_pct) == pytest.approx(10.0) and o.bench_ret_pct is not None
        assert o.confidence_bucket == "0.6-0.7"
        assert s.get(m.Recommendation, hit).status == "closed"
        assert s.get(m.Recommendation, open_).status == "active"
    sc = None
    with w.f() as s:
        sc = scorecard(s)
    assert sc["overall"]["n"] == 1 and sc["overall"]["sufficient_sample"] is False
    assert sc["by_horizon"]["short"]["target_hit_rate"] == 1.0


def test_metrics_and_xirr(w):
    assert xirr([(date(2025, 1, 1), -100), (date(2026, 1, 1), 110)]) == pytest.approx(0.10, abs=1e-4)
    pid = w.sim.create_portfolio("p", 100_000)
    w.sim.place_order(pid, "AAA", qty=100, stop=90, target=110, placed_at=evening(DAYS[0]))
    w.flat("AAA", DAYS[:2])
    w.bar("AAA", DAYS[2], 101, 112, 100, 111)
    for d in DAYS[1:3]:
        w.sim.run_eod_portfolio(pid, d)
    with w.f() as s:
        perf = performance(s, pid)
    met = perf["metrics"]
    assert met["closed_trades"] == 1 and met["hit_rate_pct"] == 100.0 and met["costs_paid"] > 0
    assert met["benchmark_return_pct"] is not None and len(perf["nav_series"]) == 2


def test_order_validation(w):
    pid = w.sim.create_portfolio("p", 100_000)
    with pytest.raises(OrderRejected):
        w.sim.place_order(pid, "AAA", qty=10, amount=1000)
    with pytest.raises(OrderRejected):
        w.sim.place_order(pid, "AAA", qty=10, stop=120, target=110)
    with pytest.raises(OrderRejected):
        w.sim.place_order(pid, "NOPE", qty=10)
