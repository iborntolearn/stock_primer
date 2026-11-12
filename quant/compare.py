"""Side-by-side comparison of 2-5 stocks (A7): price behaviour, quant scores per horizon,
fundamentals where filed, and the latest recommendation per horizon. Point-in-time at `as_of`."""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m

from . import indicators as ind
from .data import load_bars, load_index
from .engine import BENCH, QuantEngine, UnknownSymbol
from .fundamentals import last_close, load_snapshots, metrics as fund_metrics

MAX_SYMBOLS = 5
HORIZONS = ("short", "medium", "long")


def _ret(c: pd.Series, n: int) -> Optional[float]:
    return round(float(c.iloc[-1] / c.iloc[-n - 1] * 100 - 100), 2) if len(c) > n else None


def _num(x) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)


def price_stats(bars: pd.DataFrame, bench: pd.Series) -> dict[str, Any]:
    c = bars["close"]
    r = c.pct_change().dropna()
    year = c.iloc[-252:]
    hi, lo = float(year.max()), float(year.min())
    peak = year.cummax()
    beta = None
    joined = pd.concat([r.rename("s"), bench.pct_change().rename("b")], axis=1, join="inner").dropna().iloc[-250:]
    if len(joined) >= 60 and joined["b"].var() > 0:
        beta = round(float(joined["s"].cov(joined["b"]) / joined["b"].var()), 2)
    atr = ind.atr(bars).iloc[-1]
    return {
        "last": round(float(c.iloc[-1]), 2), "ret_1m_pct": _ret(c, 21), "ret_3m_pct": _ret(c, 63),
        "ret_6m_pct": _ret(c, 126), "ret_1y_pct": _ret(c, 252),
        "vol_ann_pct": round(float(r.iloc[-250:].std() * np.sqrt(252) * 100), 2) if len(r) >= 20 else None,
        "max_drawdown_1y_pct": round(float((year / peak - 1).min() * 100), 2),
        "range_52w_position": round((float(c.iloc[-1]) - lo) / (hi - lo), 3) if hi > lo else None,
        "high_52w": round(hi, 2), "low_52w": round(lo, 2),
        "atr_pct": round(float(atr / c.iloc[-1] * 100), 2) if not math.isnan(atr) else None,
        "avg_traded_value_cr": _num(bars["traded_value"].iloc[-20:].mean() / 1e7),
        "beta": beta, "above_200dma": bool(c.iloc[-1] > ind.sma(c, 200).iloc[-1]) if len(c) >= 200 else None,
    }


def shareholding_summary(s: Session, iid: int, as_of: date) -> Optional[dict[str, Any]]:
    """Latest pattern published by the end of `as_of`, with the change over about a year."""
    from datetime import datetime, time, timedelta, timezone

    cutoff = datetime.combine(as_of, time(23, 59), timezone(timedelta(hours=5, minutes=30)))
    rows = s.scalars(select(m.Shareholding).where(m.Shareholding.instrument_id == iid, m.Shareholding.published_at <= cutoff)
                     .order_by(m.Shareholding.period_end.desc()).limit(5)).all()
    if not rows:
        return None
    cur, old = rows[0], rows[-1] if len(rows) >= 4 else None

    def d(k):
        a, b = getattr(cur, k), getattr(old, k) if old else None
        return None if a is None or b is None else round(float(a) - float(b), 2)
    return {"period_end": cur.period_end.isoformat(), "promoter_pct": _num(cur.promoter_pct), "fpi_pct": _num(cur.fpi_pct),
            "dii_pct": _num(cur.dii_pct), "mf_pct": _num(cur.mf_pct), "retail_pct": _num(cur.retail_pct),
            "promoter_encumbered_pct": _num(cur.promoter_encumbered_pct),
            "promoter_change_1y": d("promoter_pct"), "fpi_change_1y": d("fpi_pct"), "dii_change_1y": d("dii_pct")}


def compare(s: Session, quant: Optional[QuantEngine], symbols: list[str], as_of: date, days: int = 250) -> dict[str, Any]:
    syms = list(dict.fromkeys(x.strip().upper() for x in symbols if x.strip()))
    if not 2 <= len(syms) <= MAX_SYMBOLS:
        raise ValueError(f"compare 2 to {MAX_SYMBOLS} symbols")
    insts = {i.symbol: i for i in s.scalars(select(m.Instrument).where(m.Instrument.symbol.in_(syms)))}
    missing = [x for x in syms if x not in insts]
    if missing:
        raise UnknownSymbol(", ".join(missing))
    sectors = {sid: name for sid, name in s.execute(select(m.Sector.id, m.Sector.name))}
    bars = load_bars(s, [insts[x].id for x in syms], as_of, 420)
    bench = load_index(s, BENCH, as_of, 420)
    snaps = load_snapshots(s, [insts[x].id for x in syms], as_of)
    rows, series = [], {}
    for sym in syms:
        inst = insts[sym]
        b = bars.get(inst.id)
        row: dict[str, Any] = {"symbol": sym, "name": inst.name, "sector": sectors.get(inst.sector_id) or inst.industry}
        if b is None or len(b) < 2:
            row["prices"] = None
        else:
            row["prices"] = price_stats(b, bench)
            c = b["close"].iloc[-days:]
            series[sym] = [{"date": d.isoformat(), "value": round(float(v / c.iloc[0] * 100), 2)} for d, v in c.items()]
        sn = snaps.get(inst.id, [])
        row["fundamentals"] = fund_metrics(sn, last_close(s, inst.id, as_of)) if sn else None
        row["shareholding"] = shareholding_summary(s, inst.id, as_of)
        scores = {}
        for h in HORIZONS:
            try:
                q = quant.score_horizon(sym, h, as_of) if quant else None
                scores[h] = None if q is None else {"quant_score": q["quant_score"], "gates_passed": q["gates_passed"],
                                                    "failed_gates": [k for k, v in q["gates"].items() if not v],
                                                    "rank": q["rank"], "universe_size": q["universe_size"]}
            except (UnknownSymbol, KeyError, ValueError):
                scores[h] = None
        row["scores"] = scores
        latest = {}
        for h in HORIZONS:
            found = s.execute(select(m.Recommendation, m.Run.decision_time).join(m.Run, m.Run.id == m.Recommendation.run_id)
                              .where(m.Recommendation.instrument_id == inst.id, m.Recommendation.horizon == h)
                              .order_by(m.Recommendation.id.desc()).limit(1)).first()
            if found is not None:
                r, decided = found
                latest[h] = {"reco_id": r.id, "verdict": r.verdict, "status": r.status, "confidence": _num(r.confidence),
                             "final_score": _num(r.final_score), "date": (r.published_at or decided).date().isoformat()}
        row["recommendations"] = latest
        rows.append(row)
    if not bench.empty:
        bc = bench.iloc[-days:]
        series[BENCH] = [{"date": d.isoformat(), "value": round(float(v / bc.iloc[0] * 100), 2)} for d, v in bc.items()]
    return {"as_of": as_of.isoformat(), "benchmark": BENCH, "items": rows, "rebased": series}
