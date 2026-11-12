"""CSV export (D11): trades, NAV and positions per portfolio, and recommendations with outcomes.
Plain numbers (no grouping, no currency symbols) so spreadsheets read them as numbers."""
from __future__ import annotations

import csv
import io
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m

DISCLAIMER = "# stock_primer export - simulated (paper) data for decision support only; not investment advice"


def _csv(header: list[str], rows: Iterable[Iterable[Any]]) -> str:
    buf = io.StringIO()
    buf.write(DISCLAIMER + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def _f(x) -> Optional[float]:
    return None if x is None else float(x)


def trades_csv(s: Session, portfolio_id: int) -> str:
    rows = s.execute(select(m.PaperTrade, m.Instrument.symbol).join(m.Instrument, m.Instrument.id == m.PaperTrade.instrument_id)
                     .where(m.PaperTrade.portfolio_id == portfolio_id).order_by(m.PaperTrade.fill_date, m.PaperTrade.id)).all()
    return _csv(["trade_id", "date", "symbol", "side", "qty", "price", "value", "brokerage", "stt", "exchange_charges", "gst",
                 "stamp_duty", "dp_charges", "slippage", "total_cost", "reason", "reco_id"],
                ([t.id, t.fill_date.isoformat(), sym, t.side, t.qty, _f(t.fill_price), round(t.qty * float(t.fill_price), 2),
                  _f(t.brokerage), _f(t.stt), _f(t.exch_charges), _f(t.gst), _f(t.stamp_duty), _f(t.dp_charges), _f(t.slippage),
                  _f(t.total_cost), t.reason, t.reco_id] for t, sym in rows))


def nav_csv(s: Session, portfolio_id: int) -> str:
    snaps = s.scalars(select(m.DailySnapshot).where(m.DailySnapshot.portfolio_id == portfolio_id)
                      .order_by(m.DailySnapshot.snap_date)).all()
    return _csv(["date", "cash", "holdings_value", "nav", "benchmark_nav"],
                ([x.snap_date.isoformat(), _f(x.cash), _f(x.holdings_value), _f(x.nav), _f(x.benchmark_nav)] for x in snaps))


def positions_csv(s: Session, portfolio_id: int) -> str:
    rows = s.execute(select(m.Position, m.Instrument.symbol).join(m.Instrument, m.Instrument.id == m.Position.instrument_id)
                     .where(m.Position.portfolio_id == portfolio_id).order_by(m.Position.opened_on)).all()
    return _csv(["symbol", "status", "qty", "avg_cost", "stop", "target", "opened_on", "expires_on", "horizon", "reco_id"],
                ([sym, p.status, p.qty, _f(p.avg_cost), _f(p.stop), _f(p.target), p.opened_on.isoformat(),
                  p.expires_on.isoformat() if p.expires_on else None, p.horizon, p.reco_id] for p, sym in rows))


def recommendations_csv(s: Session, horizon: Optional[str] = None, status: Optional[str] = None) -> str:
    q = (select(m.Recommendation, m.Instrument.symbol, m.Outcome, m.Run.decision_time)
         .join(m.Instrument, m.Instrument.id == m.Recommendation.instrument_id)
         .join(m.Run, m.Run.id == m.Recommendation.run_id)
         .outerjoin(m.Outcome, m.Outcome.reco_id == m.Recommendation.id).order_by(m.Recommendation.id))
    if horizon:
        q = q.where(m.Recommendation.horizon == horizon)
    if status:
        q = q.where(m.Recommendation.status == status)
    return _csv(["reco_id", "decided_at", "published_at", "symbol", "horizon", "verdict", "status", "quant_score", "agent_adjustment",
                 "final_score", "confidence", "entry_low", "entry_high", "stop", "target", "risk_reward", "regime",
                 "closed_at", "close_reason", "cohort", "return_pct", "benchmark_pct", "alpha_pct", "hit_target", "hit_stop", "days_held"],
                ([r.id, decided.isoformat(), r.published_at.isoformat() if r.published_at else None, sym, r.horizon, r.verdict, r.status,
                  _f(r.quant_score), _f(r.agent_adjustment), _f(r.final_score), _f(r.confidence), _f(r.entry_low), _f(r.entry_high),
                  _f(r.stop), _f(r.target), _f(r.risk_reward), r.regime_at_issue,
                  r.closed_at.isoformat() if r.closed_at else None, r.close_reason,
                  o.cohort if o else None, _f(o.ret_pct) if o else None, _f(o.bench_ret_pct) if o else None,
                  _f(o.alpha_pct) if o else None, o.hit_target if o else None, o.hit_stop if o else None, o.days_held if o else None]
                 for r, sym, o, decided in s.execute(q).all()))
