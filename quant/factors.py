"""Factor library (§4.4.1) and horizon weights (§4.4.2).

Price-led phase: quality, growth, valuation, event overlay and news are not yet
fed (no fundamentals source, no event/news scoring in quant). They score z = 0
and are reported as data gaps, which lowers confidence instead of inventing data.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from . import indicators as ind

# Bump whenever factor definitions or weights change; every run and recommendation records it.
# v2 (2026-10-14): ROE joins the quality group (balance sheets from half-yearly XBRL).
STRATEGY_VERSION = "quant-v2"

GROUPS = ("momentum", "setup", "liquidity", "quality", "growth", "valuation", "sector_market", "event", "news")

WEIGHTS: dict[str, dict[str, float]] = {
    "short": {"momentum": 0.30, "setup": 0.25, "liquidity": 0.05, "quality": 0.0, "growth": 0.0, "valuation": 0.0,
              "sector_market": 0.15, "event": 0.15, "news": 0.10},
    "medium": {"momentum": 0.25, "setup": 0.05, "liquidity": 0.05, "quality": 0.10, "growth": 0.20, "valuation": 0.05,
               "sector_market": 0.15, "event": 0.10, "news": 0.05},
    "long": {"momentum": 0.10, "setup": 0.0, "liquidity": 0.05, "quality": 0.30, "growth": 0.25, "valuation": 0.20,
             "sector_market": 0.05, "event": 0.03, "news": 0.02},
}

FACTOR_GROUPS: dict[str, tuple[str, ...]] = {
    "momentum": ("ret_1m", "ret_3m", "ret_6m", "ret_12_1", "px_vs_sma50", "px_vs_sma200", "sma50_slope",
                 "sma200_slope", "dist_52w_high"),
    "setup": ("breakout20_vol", "breakout55_vol", "pullback", "overextension"),
    "liquidity": ("log_adv20", "delivery_pct20"),
    "sector_market": ("rs_3m_vs_index", "sector_mom_3m"),
    "event": ("event_net",),
    "quality": ("op_margin", "pat_margin", "earnings_consistency", "roe"),
    "growth": ("revenue_yoy", "pat_yoy"),
    "valuation": ("earnings_yield",),
    "news": (),
}
# Fundamental factors come from quant.fundamentals.metrics (quarterly XBRL, point-in-time).
FUNDAMENTAL_KEYS = {"op_margin": "op_margin_pct", "pat_margin": "pat_margin_pct",
                    "earnings_consistency": "earnings_consistency", "revenue_yoy": "revenue_yoy_pct",
                    "pat_yoy": "pat_yoy_pct", "earnings_yield": "earnings_yield_pct", "roe": "roe_pct"}


def fundamental_factors(mt: dict) -> dict[str, float]:
    return {k: (float(mt[v]) if mt.get(v) is not None else float("nan")) for k, v in FUNDAMENTAL_KEYS.items()}
# Factors where "everyone equal" is information, not missing data (no active events -> 0, not a gap).
ZERO_WHEN_FLAT = {"event_net"}


def _f(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def raw_factors(bars: pd.DataFrame, index_close: Optional[pd.Series] = None) -> dict[str, float]:
    """Factor values for one instrument as of its last bar (sector_mom_3m is filled in cross-sectionally)."""
    if bars is None or len(bars) < 21:
        return {}
    c = bars["close"]
    s20, s50, s200 = ind.sma(c, 20), ind.sma(c, 50), ind.sma(c, 200)
    r = ind.rsi(c)
    vr = ind.volume_ratio(bars).iloc[-1]
    vr_w = min(_f(vr), 3.0) / 3.0 if not math.isnan(_f(vr)) else 0.0
    hi52 = bars["high"].rolling(252, min_periods=120).max().iloc[-1]
    last = c.iloc[-1]
    rsi_now = _f(r.iloc[-1])
    rsi_prev = _f(r.iloc[-4]) if len(r) >= 4 else float("nan")
    pullback = float(
        not math.isnan(_f(s200.iloc[-1])) and last > s200.iloc[-1] and abs(last / s20.iloc[-1] - 1) < 0.02
        and 40 <= rsi_now <= 60 and rsi_now > rsi_prev) if not math.isnan(_f(s20.iloc[-1])) else float("nan")
    out = {
        "ret_1m": _f(ind.ret(c, 21).iloc[-1]),
        "ret_3m": _f(ind.ret(c, 63).iloc[-1]) if len(c) > 63 else float("nan"),
        "ret_6m": _f(ind.ret(c, 126).iloc[-1]) if len(c) > 126 else float("nan"),
        "ret_12_1": _f(ind.ret(c, 252, skip=21).iloc[-1]) if len(c) > 252 else float("nan"),
        "px_vs_sma50": _f(last / s50.iloc[-1] - 1),
        "px_vs_sma200": _f(last / s200.iloc[-1] - 1),
        "sma50_slope": _f(ind.slope(s50).iloc[-1]),
        "sma200_slope": _f(ind.slope(s200).iloc[-1]),
        "dist_52w_high": _f(last / hi52 - 1),
        "breakout20_vol": float(bool(ind.breakout(bars, 20).iloc[-1])) * (0.5 + 0.5 * vr_w),
        "breakout55_vol": float(bool(ind.breakout(bars, 55).iloc[-1])) * (0.5 + 0.5 * vr_w) if len(c) > 55 else float("nan"),
        "pullback": pullback,
        "overextension": -max(rsi_now - 75, 0.0) if not math.isnan(rsi_now) else float("nan"),
        "log_adv20": _f(np.log1p(bars["traded_value"].rolling(20, min_periods=10).mean().iloc[-1])),
        "delivery_pct20": _f(bars["delivery_pct"].rolling(20, min_periods=5).mean().iloc[-1]),
    }
    if index_close is not None and len(index_close) > 63 and len(c) > 63:
        idx_ret = index_close.iloc[-1] / index_close.iloc[-64] - 1
        out["rs_3m_vs_index"] = _f(out["ret_3m"] - idx_ret)
    else:
        out["rs_3m_vs_index"] = float("nan")
    return out


def zscore_table(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Winsorise (1st-99th pct) and z-score each factor column cross-sectionally.

    Returns (z, stats) where stats holds the clip bounds, mean and std used, so a
    stock outside the universe can be scored against the same distribution.
    """
    z = pd.DataFrame(index=raw.index)
    stats = {}
    for col in raw.columns:
        x = raw[col].astype(float)
        valid = x.dropna()
        if len(valid) < 5 or valid.std(ddof=0) == 0:
            z[col] = 0.0 if col in ZERO_WHEN_FLAT and len(valid) else np.nan
            stats[col] = (np.nan, np.nan, np.nan, np.nan)
            continue
        lo, hi = valid.quantile(0.01), valid.quantile(0.99)
        w = x.clip(lo, hi)
        mu, sd = w.mean(), w.std(ddof=0)
        z[col] = (w - mu) / sd if sd else np.nan
        stats[col] = (lo, hi, mu, sd)
    return z, pd.DataFrame(stats, index=["lo", "hi", "mean", "std"])


def z_against(raw: dict[str, float], stats: pd.DataFrame) -> dict[str, float]:
    out = {}
    for col, v in raw.items():
        if col not in stats.columns or math.isnan(_f(v)):
            out[col] = float("nan")
            continue
        lo, hi, mu, sd = stats[col]
        out[col] = float("nan") if not sd or math.isnan(sd) else (min(max(v, lo), hi) - mu) / sd
    return out


def group_scores(z: dict[str, float]) -> tuple[dict[str, float], list[str]]:
    """Mean z per group; groups with no data score 0 and are reported as gaps."""
    scores, gaps = {}, []
    for g in GROUPS:
        vals = [z[f] for f in FACTOR_GROUPS[g] if f in z and not math.isnan(_f(z[f]))]
        if vals:
            scores[g] = float(np.mean(vals))
        else:
            scores[g] = 0.0
            gaps.append(g)
    return scores, gaps


def horizon_raw(groups: dict[str, float], horizon: str) -> float:
    return sum(w * groups.get(g, 0.0) for g, w in WEIGHTS[horizon].items())


def missing_weight(gaps: list[str], horizon: str) -> float:
    return sum(WEIGHTS[horizon][g] for g in gaps)
