"""evidence-mcp and portfolio-mcp (read side)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from core import models as m

from .registry import Out, ToolFailure, ctx, tool


@tool("evidence")
def get_evidence(evidence_id: str, as_of: str = "") -> Out:
    """Fetch a stored evidence item (E123 or 123) with its provenance."""
    eid = int(str(evidence_id).lstrip("Ee"))
    with ctx().factory() as s:
        e = s.get(m.Evidence, eid)
    if e is None:
        raise ToolFailure("NOT_FOUND", f"evidence E{eid}")
    return Out({"evidence_id": f"E{e.id}", "kind": e.kind, "source": e.source, "as_of": e.as_of.isoformat(),
                "payload": e.payload}, e.kind, e.source, as_of=e.as_of, untrusted=e.kind in ("news", "filing"))


@tool("evidence")
def search_recommendations(symbol: str = "", horizon: str = "", status: str = "", limit: int = 20,
                           as_of: str = "") -> Out:
    """Past recommendations (and outcomes where scored), newest first."""
    with ctx().factory() as s:
        q = select(m.Recommendation, m.Instrument.symbol).join(m.Instrument)
        if symbol:
            q = q.where(m.Instrument.symbol == symbol.upper())
        if horizon:
            q = q.where(m.Recommendation.horizon == horizon)
        if status:
            q = q.where(m.Recommendation.status == status)
        rows = s.execute(q.order_by(m.Recommendation.id.desc()).limit(min(int(limit), 50))).all()
        out = []
        for r, sym in rows:
            o = s.get(m.Outcome, r.id)
            out.append({"reco_id": r.id, "symbol": sym, "horizon": r.horizon, "verdict": r.verdict, "status": r.status,
                        "final_score": float(r.final_score), "confidence": float(r.confidence or 0),
                        "headline": (r.card or {}).get("headline"),
                        "outcome": {"ret_pct": float(o.ret_pct), "alpha_pct": float(o.alpha_pct or 0)} if o and o.ret_pct is not None else None})
    return Out({"items": out}, "calc", "recommendations")


def _portfolio(s, portfolio_id: int) -> m.Portfolio:
    p = s.get(m.Portfolio, int(portfolio_id))
    if p is None:
        raise ToolFailure("NOT_FOUND", f"portfolio {portfolio_id}")
    return p


@tool("portfolio")
def list_portfolios(as_of: str = "") -> Out:
    """Simulated portfolios with cash and type."""
    with ctx().factory() as s:
        rows = s.scalars(select(m.Portfolio).where(m.Portfolio.archived.is_(False))).all()
    return Out({"portfolios": [{"portfolio_id": p.id, "name": p.name, "type": p.type, "cash": float(p.cash),
                                "starting_cash": float(p.starting_cash)} for p in rows]}, "calc", "simulator")


@tool("portfolio")
def get_portfolio(portfolio_id: int, as_of: str = "") -> Out:
    """Holdings of a simulated portfolio with the originating recommendation's thesis and invalidation conditions."""
    with ctx().factory() as s:
        p = _portfolio(s, portfolio_id)
        positions = []
        for pos in s.scalars(select(m.Position).where(m.Position.portfolio_id == p.id, m.Position.status == "open")):
            inst = s.get(m.Instrument, pos.instrument_id)
            reco = s.get(m.Recommendation, pos.reco_id) if pos.reco_id else None
            positions.append({"symbol": inst.symbol, "qty": pos.qty, "avg_cost": float(pos.avg_cost),
                              "stop": float(pos.stop) if pos.stop else None,
                              "target": float(pos.target) if pos.target else None, "horizon": pos.horizon,
                              "opened_on": pos.opened_on.isoformat(),
                              "thesis": reco.thesis if reco else None, "invalidation": reco.invalidation if reco else None})
    return Out({"portfolio_id": p.id, "name": p.name, "cash": float(p.cash), "positions": positions}, "calc", "simulator")


@tool("portfolio")
def get_exposure(portfolio_id: int, as_of: str = "") -> Out:
    """Sector and horizon exposure of a simulated portfolio (by cost)."""
    with ctx().factory() as s:
        p = _portfolio(s, portfolio_id)
        by_sector, by_h, total = {}, {}, 0.0
        for pos in s.scalars(select(m.Position).where(m.Position.portfolio_id == p.id, m.Position.status == "open")):
            inst = s.get(m.Instrument, pos.instrument_id)
            v = float(pos.avg_cost) * pos.qty
            total += v
            by_sector[inst.industry or "Unknown"] = by_sector.get(inst.industry or "Unknown", 0) + v
            by_h[pos.horizon or "?"] = by_h.get(pos.horizon or "?", 0) + v
    norm = (lambda d: {k: round(v / total, 4) for k, v in d.items()}) if total else (lambda d: {})
    return Out({"portfolio_id": p.id, "by_sector": norm(by_sector), "by_horizon": norm(by_h)}, "calc", "simulator")


@tool("portfolio")
def get_performance(portfolio_id: int, from_date: str = "", to_date: str = "", as_of: str = "") -> Out:
    """NAV series and metrics of a simulated portfolio."""
    with ctx().factory() as s:
        p = _portfolio(s, portfolio_id)
        snaps = s.scalars(select(m.DailySnapshot).where(m.DailySnapshot.portfolio_id == p.id)
                          .order_by(m.DailySnapshot.snap_date)).all()
    if not snaps:
        raise ToolFailure("NOT_FOUND", f"no NAV history for portfolio {portfolio_id} yet")
    return Out({"portfolio_id": p.id, "nav": [{"date": x.snap_date.isoformat(), "nav": float(x.nav),
                                               "benchmark_nav": float(x.benchmark_nav or 0)} for x in snaps[-250:]]},
               "calc", "simulator")
