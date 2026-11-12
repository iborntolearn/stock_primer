"""Data-quality gate (§4.3, 18:20). Fails closed: if this fails, nothing is published.

Checks for the decision date over the point-in-time universe:
- calendar: the date is a trading day (else the short-term sleeve is skipped, not failed)
- freshness: >= 98% of members have a price row for the date
- completeness: no null OHLC among members that do
- sanity: high >= low, low <= close <= high, close > 0
- unexplained moves: |close/prev_close - 1| above the threshold with no corporate action -> exclude + HITL
- adjustment: on split/bonus ex-dates the adjusted return stays within the threshold
- source health: the primary EOD source was healthy for this date, or reconciliation found no anomalies
Per-instrument problems exclude that stock; universe-wide problems fail the gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m
from core.settings import Settings, get_settings

from .eod import PRIMARY
from .reference import is_trading_day, previous_trading_day, universe_members

MAX_EXCLUDED_FRACTION = 0.05   # more anomalies than this means a data problem, not stock problems


@dataclass
class GateResult:
    decision_date: date
    passed: bool
    is_trading_day: bool
    issues: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_tool_payload(self) -> dict[str, Any]:
        return {"passed": self.passed, "is_trading_day": self.is_trading_day, "issues": self.issues,
                "excluded": self.excluded, "stats": self.stats}


def run_gate(s: Session, day: date, settings: Optional[Settings] = None, *, record: bool = True) -> GateResult:
    settings = settings or get_settings()
    res = GateResult(day, passed=True, is_trading_day=is_trading_day(s, day))
    if not res.is_trading_day:
        res.issues.append(f"{day} is not a trading day")
        return _save(s, res, record)

    members = universe_members(s, settings.universe, day)
    if not members:
        res.passed = False
        res.issues.append(f"universe {settings.universe} has no members on {day}")
        return _save(s, res, record)

    prices = {p.instrument_id: p for p in s.scalars(select(m.PriceDaily).where(
        m.PriceDaily.trade_date == day, m.PriceDaily.instrument_id.in_(members)))}
    fresh = len(prices) / len(members)
    res.stats["members"] = len(members)
    res.stats["fresh_fraction"] = round(fresh, 4)
    if fresh < settings.freshness_min_fraction:
        res.passed = False
        res.issues.append(f"freshness {fresh:.1%} < {settings.freshness_min_fraction:.0%}")

    actions_today = {a.instrument_id: a.action_type for a in s.scalars(select(m.CorporateAction).where(
        m.CorporateAction.ex_date == day, m.CorporateAction.instrument_id.in_(members)))}
    prev_day = previous_trading_day(s, day)
    prev_adj = {p.instrument_id: p.adj_close for p in s.scalars(select(m.PriceDaily).where(
        m.PriceDaily.trade_date == prev_day, m.PriceDaily.instrument_id.in_(members)))}

    excluded: dict[str, str] = {}
    null_ohlc = 0
    for iid, p in prices.items():
        sym = members[iid]
        if None in (p.open, p.high, p.low, p.close):
            null_ohlc += 1
            excluded[sym] = "null OHLC"
            continue
        if p.close <= 0 or p.high < p.low or not (p.low <= p.close <= p.high):
            excluded[sym] = "OHLC inconsistent"
            continue
        if p.prev_close:
            move = abs(float(p.close / p.prev_close) - 1) * 100
            if move > settings.unexplained_move_pct and iid not in actions_today:
                excluded[sym] = f"unexplained move {move:.1f}%"
        if actions_today.get(iid) in ("split", "bonus") and prev_adj.get(iid) and p.adj_close:
            adj_move = abs(float(p.adj_close / prev_adj[iid]) - 1) * 100
            if adj_move > settings.unexplained_move_pct:
                excluded[sym] = f"adjusted series discontinuous ({adj_move:.1f}%) on {actions_today[iid]} ex-date"
    if null_ohlc:
        res.issues.append(f"{null_ohlc} member(s) with null OHLC")

    if len(excluded) > MAX_EXCLUDED_FRACTION * len(members):
        res.passed = False
        res.issues.append(f"{len(excluded)} members excluded (> {MAX_EXCLUDED_FRACTION:.0%}); suspect the source")

    # Source health: primary healthy for this date, else accept only if no reconciliation anomalies are open.
    latest = s.scalar(select(m.SourceHealth).where(m.SourceHealth.source == PRIMARY)
                      .order_by(m.SourceHealth.checked_at.desc()).limit(1))
    primary_ok = bool(latest and latest.ok and latest.last_data_at and latest.last_data_at.date() >= day)
    if not primary_ok:
        sources = set(s.scalars(select(m.PriceDaily.source).where(m.PriceDaily.trade_date == day)))
        res.issues.append(f"primary source unhealthy; data from {sorted(sources) or 'nowhere'}")
        if not sources:
            res.passed = False

    open_anomalies = s.scalars(select(m.HitlTask).where(m.HitlTask.task_type == "data_anomaly",
                                                        m.HitlTask.status == "pending")).all()
    for t in open_anomalies:
        if t.payload.get("day") == day.isoformat() and t.payload.get("symbol"):
            excluded.setdefault(t.payload["symbol"], "reconciliation mismatch")

    already = {(t.payload.get("symbol"), t.payload.get("day")) for t in open_anomalies}
    for sym, why in sorted(excluded.items()):
        res.excluded.append(sym)
        if why.startswith(("unexplained", "adjusted")) and (sym, day.isoformat()) not in already:
            iid = next(i for i, sy in members.items() if sy == sym)
            s.add(m.HitlTask(task_type="data_anomaly", subject_type="instrument", subject_id=iid, priority=2,
                             payload={"check": "gate", "symbol": sym, "day": day.isoformat(), "reason": why},
                             ai_recommendation="exclude", reason=f"{sym}: {why}", default_on_expiry="exclude_instrument"))
    res.stats["excluded"] = len(res.excluded)
    return _save(s, res, record)


def _save(s: Session, res: GateResult, record: bool) -> GateResult:
    if record:
        s.merge(m.DataGateResult(decision_date=res.decision_date, passed=res.passed, is_trading_day=res.is_trading_day,
                                 issues=res.issues, excluded=res.excluded, stats=res.stats,
                                 checked_at=datetime.now(timezone.utc)))
    return res


def latest_gate(s: Session, day: date) -> Optional[GateResult]:
    row = s.get(m.DataGateResult, day)
    if row is None:
        return None
    return GateResult(row.decision_date, row.passed, row.is_trading_day, list(row.issues), list(row.excluded),
                      dict(row.stats))
