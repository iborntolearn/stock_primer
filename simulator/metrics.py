"""Portfolio performance and risk metrics (feature D7), computed on read."""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m


def xirr(flows: list[tuple[date, float]], guess: float = 0.1) -> Optional[float]:
    """Annualised IRR for dated cash flows (negative = money in). Newton's method with bisection fallback."""
    if len(flows) < 2 or not any(f < 0 for _, f in flows) or not any(f > 0 for _, f in flows):
        return None
    t0 = flows[0][0]
    years = [((d - t0).days / 365.0, f) for d, f in flows]

    def npv(r: float) -> float:
        return sum(f / (1 + r) ** t for t, f in years)

    lo, hi = -0.9999, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-9:
            break
    return (lo + hi) / 2


def performance(s: Session, portfolio_id: int, start: Optional[date] = None, end: Optional[date] = None) -> dict[str, Any]:
    p = s.get(m.Portfolio, portfolio_id)
    q = select(m.DailySnapshot).where(m.DailySnapshot.portfolio_id == portfolio_id)
    if start:
        q = q.where(m.DailySnapshot.snap_date >= start)
    if end:
        q = q.where(m.DailySnapshot.snap_date <= end)
    snaps = s.scalars(q.order_by(m.DailySnapshot.snap_date)).all()
    trades = s.scalars(select(m.PaperTrade).where(m.PaperTrade.portfolio_id == portfolio_id)
                       .order_by(m.PaperTrade.fill_date, m.PaperTrade.id)).all()
    out: dict[str, Any] = {"portfolio_id": portfolio_id, "name": p.name, "type": p.type,
                           "starting_cash": float(p.starting_cash), "cash": float(p.cash), "benchmark": p.benchmark,
                           "nav_series": [{"date": x.snap_date.isoformat(), "nav": float(x.nav),
                                           "benchmark_nav": float(x.benchmark_nav) if x.benchmark_nav is not None else None}
                                          for x in snaps]}
    if not snaps:
        return out | {"metrics": None}
    navs = [float(x.nav) for x in snaps]
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs)) if navs[i - 1]]
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    total = navs[-1] / float(p.starting_cash) - 1
    bench = [float(x.benchmark_nav) for x in snaps if x.benchmark_nav is not None]
    bench_ret = bench[-1] / float(p.starting_cash) - 1 if bench else None
    # Round trips: pair each SELL with its BUY lot by instrument (FIFO).
    lots: dict[int, list[m.PaperTrade]] = {}
    wins, losses = [], []
    for t in trades:
        if t.side == "BUY":
            lots.setdefault(t.instrument_id, []).append(t)
        elif lots.get(t.instrument_id):
            b = lots[t.instrument_id].pop(0)
            pnl = (float(t.fill_price) - float(b.fill_price)) * t.qty - float(b.total_cost or 0) - float(t.total_cost or 0)
            (wins if pnl > 0 else losses).append(pnl / (float(b.fill_price) * b.qty))
    closed = len(wins) + len(losses)
    out["metrics"] = {
        "nav": navs[-1], "total_return_pct": round(total * 100, 2),
        "benchmark_return_pct": round(bench_ret * 100, 2) if bench_ret is not None else None,
        "alpha_pct": round((total - bench_ret) * 100, 2) if bench_ret is not None else None,
        "xirr_pct": (lambda r: round(r * 100, 2) if r is not None else None)(
            xirr([(snaps[0].snap_date, -float(p.starting_cash)), (snaps[-1].snap_date, navs[-1])])
            if (snaps[-1].snap_date - snaps[0].snap_date).days >= 30 else None),
        "max_drawdown_pct": round(mdd * 100, 2),
        "volatility_ann_pct": round(_std(rets) * math.sqrt(252) * 100, 2) if len(rets) > 1 else None,
        "closed_trades": closed, "hit_rate_pct": round(100 * len(wins) / closed, 1) if closed else None,
        "avg_win_pct": round(100 * sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(100 * sum(losses) / len(losses), 2) if losses else None,
        "costs_paid": round(sum(float(t.total_cost or 0) for t in trades), 2), "days": len(snaps),
    }
    return out


def _std(x: list[float]) -> float:
    mu = sum(x) / len(x)
    return math.sqrt(sum((v - mu) ** 2 for v in x) / (len(x) - 1))
