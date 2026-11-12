"""Score recommendations as they mature (§4.2 nightly evaluation_run, F4, §13.2).

Each recommendation is followed like the auto-follow portfolio would: entry at
the next session's open after the reference time, then daily stop -> target ->
horizon expiry -> thesis break. Prices are split/bonus adjusted; the card's
levels are rescaled to the same basis; the benchmark is the Nifty 500 over the
same window.

Cohorts (the "do humans add value?" test):
- published: picks that went out (auto or approved in review); reference = published_at
- human_rejected / expired: picks a reviewer rejected, or that expired undecided;
  reference = the decision time. Scored hypothetically; their status never changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m
from quant.data import load_bars, load_index
from simulator.engine import DEFAULT_RULES, IST

BENCH = "NIFTY 500"


@dataclass
class OutcomeReport:
    day: date
    scored: list[str] = field(default_factory=list)
    activated: list[str] = field(default_factory=list)


def _bucket(conf: Optional[float]) -> str:
    if conf is None:
        return "unknown"
    lo = min(int(conf * 10), 9) / 10
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def _follow(s: Session, r: m.Recommendation, ref: date, day: date, invalidated: bool) -> Optional[dict]:
    """Walk the price path after `ref`; return the exit or {'open': True} if still running."""
    bars = load_bars(s, [r.instrument_id], day, (day - ref).days + 5).get(r.instrument_id)
    if bars is None:
        return None
    path = bars[bars.index > ref]
    if path.empty:
        return None
    entry_raw = s.get(m.PriceDaily, (r.instrument_id, path.index[0]))
    factor = float(entry_raw.adj_close / entry_raw.close) if entry_raw and entry_raw.adj_close and entry_raw.close else 1.0
    stop = float(r.stop) * factor if r.stop is not None else None
    target = float(r.target) * factor if r.target is not None else None
    entry = float(path["open"].iloc[0])
    expiry = ref + timedelta(days=DEFAULT_RULES["holding_days"].get(r.horizon, 28))
    lo_seen = hi_seen = entry
    for d, b in path.iterrows():
        lo_seen, hi_seen = min(lo_seen, b["low"]), max(hi_seen, b["high"])
        exit_px = reason = None
        if target is not None and b["open"] >= target:
            exit_px, reason = b["open"], "target"
        elif stop is not None and b["low"] <= stop:
            exit_px, reason = min(b["open"], stop), "stop"
        elif target is not None and b["high"] >= target:
            exit_px, reason = target, "target"
        elif d >= expiry:
            exit_px, reason = b["close"], "horizon_end"
        elif invalidated:
            exit_px, reason = b["close"], "invalidated"
        if exit_px is not None:
            return {"entry_day": path.index[0], "exit_day": d, "entry": entry, "exit": exit_px, "reason": reason,
                    "lo": lo_seen, "hi": hi_seen}
    return {"open": True}


def _outcome(s: Session, r: m.Recommendation, f: dict, bench, cohort: str) -> m.Outcome:
    b0 = bench[bench.index < f["entry_day"]]
    b1 = bench[bench.index <= f["exit_day"]]
    bench_ret = (b1.iloc[-1] / b0.iloc[-1] - 1) * 100 if len(b0) and len(b1) else None
    ret = (f["exit"] / f["entry"] - 1) * 100
    return m.Outcome(
        reco_id=r.id, evaluated_at=datetime.now(timezone.utc), ret_pct=Decimal(f"{ret:.4f}"),
        bench_ret_pct=Decimal(f"{bench_ret:.4f}") if bench_ret is not None else None,
        alpha_pct=Decimal(f"{ret - bench_ret:.4f}") if bench_ret is not None else None,
        max_adverse_pct=Decimal(f"{(f['lo'] / f['entry'] - 1) * 100:.4f}"),
        max_favourable_pct=Decimal(f"{(f['hi'] / f['entry'] - 1) * 100:.4f}"),
        hit_target=f["reason"] == "target", hit_stop=f["reason"] == "stop",
        days_held=(f["exit_day"] - f["entry_day"]).days,
        confidence_bucket=_bucket(float(r.confidence) if r.confidence is not None else None), cohort=cohort)


def score_outcomes(factory: sessionmaker[Session], day: date) -> OutcomeReport:
    rep = OutcomeReport(day)
    with factory() as s, s.begin():
        bench = load_index(s, BENCH, day, 1200)
        scored = set(s.scalars(select(m.Outcome.reco_id)))
        # Published cohort
        for r in s.scalars(select(m.Recommendation).where(
                m.Recommendation.status.in_(("published", "active", "invalidated")),
                m.Recommendation.published_at.is_not(None))).all():
            if r.id in scored:
                continue
            ref = r.published_at.astimezone(IST).date()
            if ref >= day:
                continue
            sym = s.get(m.Instrument, r.instrument_id).symbol
            fol = _follow(s, r, ref, day, invalidated=r.status == "invalidated")
            if fol is None:
                continue
            if fol.get("open"):
                if r.status == "published":
                    r.status = "active"
                    rep.activated.append(sym)
                continue
            s.add(_outcome(s, r, fol, bench, "published"))
            r.status = "expired" if fol["reason"] == "horizon_end" else "closed"
            r.closed_at = datetime.combine(fol["exit_day"], time(15, 30), IST)
            r.close_reason = fol["reason"]
            rep.scored.append(f"{sym}/{r.horizon} {fol['reason']} {(fol['exit'] / fol['entry'] - 1) * 100:+.2f}%")
        # Counterfactual cohorts: rejected in review, or expired undecided
        for t, r in s.execute(select(m.HitlTask, m.Recommendation).join(
                m.Recommendation, m.Recommendation.id == m.HitlTask.subject_id).where(
                m.HitlTask.task_type == "approve_reco", m.HitlTask.subject_type == "recommendation",
                m.HitlTask.status.in_(("rejected", "expired")), m.Recommendation.status == "rejected",
                m.Recommendation.published_at.is_(None))).all():
            if r.id in scored or t.decided_at is None:
                continue
            ref = t.decided_at.astimezone(IST).date()
            if ref >= day:
                continue
            fol = _follow(s, r, ref, day, invalidated=False)
            if fol is None or fol.get("open"):
                continue
            cohort = "expired" if t.status == "expired" else "human_rejected"
            s.add(_outcome(s, r, fol, bench, cohort))
            scored.add(r.id)
            rep.scored.append(f"{s.get(m.Instrument, r.instrument_id).symbol}/{r.horizon} [{cohort}] {fol['reason']}")
    return rep
