"""Intraday stop-proximity alerts (Phase 5, optional; §10 notification table).

Watches open simulated positions and live favourable picks. For each, compares a delayed
intraday quote against its stop and target:
- stop_breach: the day's low reached the stop
- near_stop:   last price within `proximity_pct` of the stop (skipped once breached)
- target_hit:  the day's high reached the target
Each (day, kind, subject) alerts once (alert_log). Alerts only; nothing is traded here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from core import models as m


@dataclass
class Watch:
    key: str
    symbol: str
    label: str
    stop: Optional[float]
    target: Optional[float]


def watchlist(s: Session) -> list[Watch]:
    out = []
    for pos, inst, port in s.execute(select(m.Position, m.Instrument, m.Portfolio)
                                     .join(m.Instrument, m.Instrument.id == m.Position.instrument_id)
                                     .join(m.Portfolio, m.Portfolio.id == m.Position.portfolio_id)
                                     .where(m.Position.status == "open", m.Portfolio.archived.is_(False))):
        if pos.stop is not None or pos.target is not None:
            out.append(Watch(f"pos:{port.id}:{inst.symbol}", inst.symbol, f"{port.name} position",
                             float(pos.stop) if pos.stop is not None else None,
                             float(pos.target) if pos.target is not None else None))
    for r, inst in s.execute(select(m.Recommendation, m.Instrument).join(m.Instrument, m.Instrument.id == m.Recommendation.instrument_id)
                             .where(m.Recommendation.status.in_(("published", "active")), m.Recommendation.verdict == "favourable")):
        out.append(Watch(f"reco:{r.id}", inst.symbol, f"{r.horizon}-term pick #{r.id}",
                         float(r.stop) if r.stop is not None else None, float(r.target) if r.target is not None else None))
    return out


def evaluate(w: Watch, last: float, day_high: float, day_low: float, proximity_pct: float) -> list[tuple[str, float, float]]:
    """(kind, price, level) alerts for one watched item."""
    hits = []
    if w.stop is not None:
        if day_low <= w.stop:
            hits.append(("stop_breach", day_low, w.stop))
        elif last <= w.stop * (1 + proximity_pct / 100):
            hits.append(("near_stop", last, w.stop))
    if w.target is not None and day_high >= w.target:
        hits.append(("target_hit", day_high, w.target))
    return hits


TEXT = {
    "stop_breach": "⛔ {sym} touched its stop: low ₹{price:,.2f} vs stop ₹{level:,.2f} ({label}).",
    "near_stop": "⚠️ {sym} is {gap:.1f}% above its stop: ₹{price:,.2f} vs stop ₹{level:,.2f} ({label}).",
    "target_hit": "🎯 {sym} reached its target: high ₹{price:,.2f} vs target ₹{level:,.2f} ({label}).",
}


def check(f: sessionmaker[Session], quotes: dict[str, Any], day: date, proximity_pct: float,
          now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Record and return new alerts. `quotes` maps symbol -> object with last/day_high/day_low/at."""
    with f() as s:
        items = watchlist(s)
        sent = {(a.kind, a.subject_key) for a in s.scalars(select(m.AlertLog).where(m.AlertLog.alert_date == day))}
    new = []
    for w in items:
        q = quotes.get(w.symbol)
        if q is None:
            continue
        for kind, price, level in evaluate(w, q.last, q.day_high, q.day_low, proximity_pct):
            if (kind, w.key) in sent or (kind == "near_stop" and ("stop_breach", w.key) in sent):
                continue
            text = TEXT[kind].format(sym=w.symbol, price=price, level=level, label=w.label,
                                     gap=(price / level - 1) * 100 if level else 0.0)
            text += f" Quote {q.at:%H:%M} IST, delayed. Simulated portfolios still act at end of day."
            try:
                with f() as s, s.begin():
                    s.add(m.AlertLog(alert_date=day, kind=kind, subject_key=w.key, symbol=w.symbol,
                                     price=Decimal(str(round(price, 4))), level=Decimal(str(round(level, 4))),
                                     payload={"label": w.label, "quote_at": q.at.isoformat()}, **({"sent_at": now} if now else {})))
            except IntegrityError:
                continue                              # another worker sent it first
            sent.add((kind, w.key))
            new.append({"kind": kind, "symbol": w.symbol, "subject": w.key, "price": price, "level": level, "text": text})
    return new
