"""Scorecard (S10, §7.5, §13.2). Sample sizes sit next to every metric and nothing is judged
below MIN_SAMPLE - small samples are noise.

Answers the doc's decision questions:
- better than the benchmark?          overall / by horizon (published picks only)
- do agents add value over quant?     auto-follow vs quant-only shadow portfolio; per-agent attribution
- is confidence meaningful?           calibration buckets
- do humans add value?                published vs human_rejected vs expired cohorts
- are results regime-dependent?       attribution by regime at entry
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core import models as m
from simulator.metrics import performance

MIN_SAMPLE = 20


def summary(items: list) -> dict[str, Any]:
    n = len(items)
    alphas = [float(o.alpha_pct) for _, o in items if o.alpha_pct is not None]
    return {"n": n, "sufficient_sample": n >= MIN_SAMPLE,
            "positive_alpha_rate": round(sum(a > 0 for a in alphas) / len(alphas), 3) if alphas else None,
            "target_hit_rate": round(sum(bool(o.hit_target) for _, o in items) / n, 3) if n else None,
            "stop_hit_rate": round(sum(bool(o.hit_stop) for _, o in items) / n, 3) if n else None,
            "avg_return_pct": round(mean(float(o.ret_pct) for _, o in items), 2) if n else None,
            "avg_alpha_pct": round(mean(alphas), 2) if alphas else None,
            "avg_days_held": round(mean(o.days_held or 0 for _, o in items), 1) if n else None}


def _group(items: Iterable, key) -> dict[str, dict]:
    g: dict[str, list] = defaultdict(list)
    for x in items:
        g[key(x)].append(x)
    return {k: summary(v) for k, v in sorted(g.items())}


def _agent_attribution(items: list) -> list[dict]:
    """For each signal source, compare outcomes where it pushed the score up vs down."""
    by: dict[tuple[str, str], list] = defaultdict(list)
    for r, o in items:
        for c in (r.meta or {}).get("contributions") or []:
            pts = c.get("points") or 0
            if pts:
                by[(c["source"], "raised" if pts > 0 else "lowered")].append((r, o))
    out = []
    for source in sorted({k[0] for k in by}):
        out.append({"source": source, "raised": summary(by.get((source, "raised"), [])),
                    "lowered": summary(by.get((source, "lowered"), []))})
    return out


def scorecard(s: Session) -> dict[str, Any]:
    rows = s.execute(select(m.Recommendation, m.Outcome, m.Instrument)
                     .join(m.Outcome, m.Outcome.reco_id == m.Recommendation.id)
                     .join(m.Instrument, m.Instrument.id == m.Recommendation.instrument_id)).all()
    sectors = {sid: name for sid, name in s.execute(select(m.Sector.id, m.Sector.name))}
    taken = [(r, o) for r, o, _ in rows if (o.cohort or "published") == "published"]
    sector_of = {r.id: sectors.get(i.sector_id, i.industry or "Unknown") for r, _, i in rows}

    daily_runs = s.scalars(select(m.Run).where(m.Run.run_type.in_(("daily", "weekly", "monthly")),
                                               m.Run.status.in_(("succeeded", "degraded")))).all()
    published_by_run = dict(s.execute(select(m.Recommendation.run_id, func.count()).where(
        m.Recommendation.published_at.is_not(None)).group_by(m.Recommendation.run_id)).all())
    no_pick = sum(1 for r in daily_runs if not published_by_run.get(r.id))
    spend = float(s.scalar(select(func.coalesce(func.sum(m.LlmCall.cost_usd), 0))) or 0)
    published = int(s.scalar(select(func.count()).select_from(m.Recommendation)
                             .where(m.Recommendation.published_at.is_not(None))) or 0)
    portfolios = [performance(s, p.id) for p in s.scalars(select(m.Portfolio).where(m.Portfolio.archived.is_(False)))]

    def by_type(t: str):
        return next((p for p in portfolios if p["type"] == t and p["metrics"]), None)
    auto, shadow = by_type("auto_follow"), by_type("quant_shadow")
    value_add = None
    if auto and shadow:
        a, q = auto["metrics"], shadow["metrics"]
        value_add = {"auto_follow_return_pct": a["total_return_pct"], "quant_shadow_return_pct": q["total_return_pct"],
                     "agents_minus_quant_pp": round(a["total_return_pct"] - q["total_return_pct"], 2),
                     "auto_follow_trades": a["closed_trades"], "quant_shadow_trades": q["closed_trades"],
                     "sufficient_sample": min(a["closed_trades"], q["closed_trades"]) >= MIN_SAMPLE}

    cohorts = _group([(r, o) for r, o, _ in rows], lambda x: x[1].cohort or "published")
    return {
        "min_sample": MIN_SAMPLE,
        "overall": summary(taken),
        "by_horizon": _group(taken, lambda x: x[0].horizon),
        "calibration": [{"bucket": b, **v, "mean_confidence": round(mean(float(r.confidence or 0) for r, o in taken
                                                                         if (o.confidence_bucket or "unknown") == b), 3)}
                        for b, v in _group(taken, lambda x: x[1].confidence_bucket or "unknown").items()],
        "attribution": {
            "by_sector": _group(taken, lambda x: sector_of.get(x[0].id, "Unknown")),
            "by_regime": _group(taken, lambda x: x[0].regime_at_issue or "unknown"),
            "by_agent": _agent_attribution(taken),
        },
        "human_review": cohorts,
        "value_add": value_add,
        "abstention": {"runs": len(daily_runs), "runs_without_picks": no_pick,
                       "rate": round(no_pick / len(daily_runs), 3) if daily_runs else None},
        "cost": {"llm_spend_usd": round(spend, 2), "published": published,
                 "usd_per_published": round(spend / published, 3) if published else None},
        "portfolios": [{"portfolio_id": p["portfolio_id"], "name": p["name"], "type": p["type"],
                        "metrics": p["metrics"], "nav_series": p["nav_series"][-250:]} for p in portfolios],
    }
