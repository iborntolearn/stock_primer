"""Quant engine on synthetic, deterministic price histories."""
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from core import models as m
from core.db import make_engine, session_factory
from core.settings import Settings
from ingestion.reference import sync_universe
from quant import indicators as ind
from quant.engine import QuantEngine, UnknownSymbol, tick
from quant.factors import zscore_table

AS_OF = date(2026, 10, 12)


def trading_days(n: int, end: date = AS_OF) -> list[date]:
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


# -- indicator golden values ------------------------------------------------------
def test_rsi_extremes_and_midpoint():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert ind.rsi(up).iloc[-1] == pytest.approx(100.0)
    zigzag = pd.Series([10.0, 11.0] * 30)
    assert ind.rsi(zigzag).iloc[-1] == pytest.approx(50.0, abs=5)


def test_atr_of_constant_range():
    df = pd.DataFrame({"high": [11.0] * 30, "low": [9.0] * 30, "close": [10.0] * 30})
    assert ind.atr(df).iloc[-1] == pytest.approx(2.0)


def test_breakout_uses_prior_highs_only():
    df = pd.DataFrame({"high": [10.0] * 21 + [12.0], "close": [9.5] * 21 + [11.0], "low": [9.0] * 22})
    assert bool(ind.breakout(df, 20).iloc[-1])
    df.loc[21, "close"] = 9.9
    assert not bool(ind.breakout(df, 20).iloc[-1])


def test_twelve_minus_one_momentum_skips_last_month():
    c = pd.Series(np.r_[np.full(253, 100.0), np.full(21, 200.0)])
    assert ind.ret(c, 252, skip=21).iloc[-1] == pytest.approx(0.0)


def test_zscore_winsorises_outliers():
    raw = pd.DataFrame({"f": [1.0] * 150 + [2.0] * 149 + [1000.0]})   # 1 outlier in 300 is above the 99th pct
    z, stats = zscore_table(raw)
    assert z["f"].max() < 3 and abs(z["f"].mean()) < 1e-9


def test_tick_rounding():
    assert tick(101.03) == 101.05 and tick(101.02) == 101.0


# -- engine on a synthetic universe ----------------------------------------------------
def build_universe(n_up=10, n_flat=10, n_down=10, days=300):
    engine = make_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    f = session_factory(engine)
    rng = np.random.default_rng(7)
    dates = trading_days(days)
    names = [f"UP{i}" for i in range(n_up)] + [f"FLAT{i}" for i in range(n_flat)] + [f"DN{i}" for i in range(n_down)]
    with f() as s, s.begin():
        sync_universe(s, "NIFTY200", [{"symbol": x, "name": x, "industry": "Up" if x.startswith("UP") else "Other",
                                       "series": "EQ", "isin": ""} for x in names], dates[0])
        iids = {sym: s.scalar(m.Instrument.__table__.select().with_only_columns(m.Instrument.id)
                              .where(m.Instrument.symbol == sym)) for sym in names}
        for sym in names:
            drift = 0.003 if sym.startswith("UP") else -0.003 if sym.startswith("DN") else 0.0
            px = 500 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, days)))
            for d, p in zip(dates, px):
                p = Decimal(str(round(p, 2)))
                s.add(m.PriceDaily(instrument_id=iids[sym], trade_date=d, open=p, high=p * Decimal("1.01"),
                                   low=p * Decimal("0.99"), close=p, adj_close=p, prev_close=p,
                                   volume=1_000_000, traded_value=p * 1_000_000, delivery_pct=Decimal("45"),
                                   source="test"))
        idx = 20000 * np.exp(np.cumsum(0.0005 + rng.normal(0, 0.005, days)))
        for d, v in zip(dates, idx):
            s.add(m.IndexPriceDaily(index_code="NIFTY 500", trade_date=d, close=Decimal(str(round(v, 2)))))
        for d in dates[-10:]:
            s.add(m.IndexPriceDaily(index_code="INDIA VIX", trade_date=d, close=Decimal("13.5")))
    return QuantEngine(f, Settings(universe="NIFTY200", min_avg_traded_value_cr=10, min_price=50)), f


@pytest.fixture(scope="module")
def qe():
    return build_universe()[0]


def test_uptrending_stocks_score_higher(qe):
    up = [qe.score_horizon(f"UP{i}", "short", AS_OF)["quant_score"] for i in range(10)]
    dn = [qe.score_horizon(f"DN{i}", "short", AS_OF)["quant_score"] for i in range(10)]
    assert min(up) > max(dn)                       # every uptrend outranks every downtrend
    assert min(up) >= 60 and max(dn) <= 34         # top and bottom thirds of a 30-stock universe
    up, dn = qe.score_horizon("UP0", "short", AS_OF), qe.score_horizon("DN0", "short", AS_OF)
    assert up["above_200dma"] is True and dn["above_200dma"] is False


def test_contract_fields(qe):
    r = qe.score_horizon("UP1", "short", AS_OF)
    for k in ("symbol", "sector", "quant_score", "confidence", "gates", "gates_passed", "rank", "above_200dma",
              "atr", "sector_index", "data_gaps"):
        assert k in r
    assert set(r["data_gaps"]) == {"quality", "growth", "valuation", "news"}   # event overlay is live (0 when no events)


def test_confidence_reflects_missing_fundamentals(qe):
    short = qe.score_horizon("UP0", "short", AS_OF)["confidence"]
    long = qe.score_horizon("UP0", "long", AS_OF)["confidence"]
    assert short > long


def test_long_gate_requires_price_above_200dma(qe):
    assert qe.score_horizon("DN0", "long", AS_OF)["gates"]["above_200dma"] is False
    assert qe.score_horizon("DN0", "long", AS_OF)["gates_passed"] is False


def test_screen_returns_eligible_ranked(qe):
    out = qe.screen_universe("short", AS_OF, top_k=5)
    syms = [c["symbol"] for c in out["candidates"]]
    assert len(syms) == 5 and all(s.startswith("UP") for s in syms)
    scores = [c["screen_score"] for c in out["candidates"]]
    assert scores == sorted(scores, reverse=True)
    sector_only = qe.screen_universe("short", AS_OF, top_k=50, constraints={"sector": "Other"})
    assert all(not c["symbol"].startswith("UP") for c in sector_only["candidates"])


@pytest.mark.parametrize("horizon,floor", [("short", 1.5), ("medium", 2.0), ("long", 1.5)])
def test_levels_are_ordered_and_meet_rr_floor(qe, horizon, floor):
    lv = qe.propose_levels("UP2", horizon, AS_OF)
    assert lv["stop"] < lv["entry_low"] <= lv["entry_high"] < lv["target"]
    assert lv["rr"] >= floor
    for k in ("entry_low", "entry_high", "stop", "target"):
        assert round(lv[k] / 0.05, 6) == round(lv[k] / 0.05)
    if horizon == "short":
        dist = lv["entry_low"] - lv["stop"]
        assert 0.5 * lv["atr"] <= dist <= 4 * lv["atr"]   # validation V4 band


def test_regime_and_breadth(qe):
    r = qe.regime_inputs(AS_OF)
    assert r["quant_label"] in ("risk_on", "neutral", "risk_off") and "score" in r
    assert r["india_vix"] == 13.5
    b = qe.market_breadth(AS_OF)
    assert b["advances"] + b["declines"] <= 30 and b["pct_above_200dma"] is not None


def test_unknown_symbol(qe):
    with pytest.raises(UnknownSymbol):
        qe.score_horizon("NOPE", "short", AS_OF)


def test_illiquid_stock_fails_gate():
    qe, f = build_universe(n_up=3, n_flat=3, n_down=3, days=260)
    with f() as s, s.begin():
        iid = s.scalar(m.Instrument.__table__.select().with_only_columns(m.Instrument.id).where(m.Instrument.symbol == "UP0"))
        for p in s.query(m.PriceDaily).filter(m.PriceDaily.instrument_id == iid):
            p.traded_value = Decimal("1000")
    r = qe.score_horizon("UP0", "short", AS_OF)
    assert r["gates"]["liquidity"] is False and not r["gates_passed"]


def test_position_size():
    out = QuantEngine.suggest_position_size(1_000_000, entry=100, stop=95)
    assert out["qty"] == 1000 and out["binding"] == "position_cap"
