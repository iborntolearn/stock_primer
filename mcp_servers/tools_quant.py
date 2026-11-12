"""quant-mcp and macro-mcp: indicators, factor/horizon scores, screening, levels, regime inputs."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from core import models as m
from quant.engine import UnknownSymbol

from .registry import Out, ToolFailure, ctx, decision_date, tool


def _q(fn, *a):
    try:
        return fn(*a)
    except UnknownSymbol as e:
        raise ToolFailure("NOT_FOUND", str(e)) from e


def _horizon(h: str) -> str:
    if h not in ("short", "medium", "long"):
        raise ToolFailure("INVALID_ARGUMENT", "horizon must be short, medium or long")
    return h


@tool("quant")
def compute_indicators(symbol: str, indicators: Optional[list[str]] = None, as_of: str = "") -> Out:
    """RSI, ATR, moving averages and slopes, breakout flags, volume ratio, 52-week range, returns - computed server-side."""
    out = _q(ctx().quant.compute_indicators, symbol.upper(), decision_date(as_of))
    if indicators:
        wanted = {i.upper() for i in indicators}
        out = {k: v for k, v in out.items() if k in ("symbol", "as_of", "close", "bars") or k.upper() in wanted
               or any(k.upper().startswith(w) for w in wanted)}
    return Out(out, "indicator", "quant_engine")


@tool("quant")
def get_factor_scores(symbol: str, as_of: str = "") -> Out:
    """Cross-sectional factor z-scores, group scores and data-gap flags for a symbol."""
    return Out(_q(ctx().quant.factor_scores, symbol.upper(), decision_date(as_of)), "calc", "quant_engine")


@tool("quant")
def score_horizon(symbol: str, horizon: str, as_of: str = "") -> Out:
    """Quant score (0-100 percentile in the universe), confidence, hard-gate results and rank for one horizon."""
    return Out(_q(ctx().quant.score_horizon, symbol.upper(), _horizon(horizon), decision_date(as_of)),
               "calc", "quant_engine")


@tool("quant")
def screen_universe(horizon: str, top_k: int = 15, constraints: Optional[dict] = None, as_of: str = "") -> Out:
    """Ranked gate-passing candidates for a horizon, with scores and counts of gate rejections."""
    return Out(ctx().quant.screen_universe(_horizon(horizon), decision_date(as_of), min(int(top_k), 50), constraints),
               "calc", "quant_engine")


@tool("quant")
def propose_levels(symbol: str, horizon: str, as_of: str = "") -> Out:
    """Entry zone, stop, target and reward:risk for a horizon (deterministic; agents may not change them)."""
    return Out(_q(ctx().quant.propose_levels, symbol.upper(), _horizon(horizon), decision_date(as_of)),
               "calc", "quant_engine")


@tool("quant")
def suggest_position_size(portfolio_value: float, entry: float, stop: float, risk_pct: float = 0.75,
                          max_position_pct: float = 10.0, as_of: str = "") -> Out:
    """Position size suggestion for a paper portfolio from risk-per-trade and a position cap."""
    return Out(ctx().quant.suggest_position_size(portfolio_value, entry, stop, risk_pct, max_position_pct),
               "calc", "quant_engine")


@tool("macro")
def get_regime_inputs(as_of: str = "") -> Out:
    """Inputs behind the market regime (Nifty 500 trend, breadth, India VIX, ...) and the quant regime label."""
    r = ctx().quant.regime_inputs(decision_date(as_of))
    return Out(r, "macro", "quant_engine", data_quality="degraded" if r.get("unavailable") else "ok",
               warnings=[f"not yet ingested: {', '.join(r['unavailable'])}"] if r.get("unavailable") else [])


@tool("macro")
def get_flows(from_date: str = "", to_date: str = "", as_of: str = "") -> Out:
    """FII/DII provisional net flows."""
    with ctx().factory() as s:
        rows = s.scalars(select(m.FlowsDaily).where(m.FlowsDaily.trade_date <= decision_date(as_of))
                         .order_by(m.FlowsDaily.trade_date.desc()).limit(10)).all()
    if not rows:
        raise ToolFailure("NOT_FOUND", "FII/DII flows are not ingested yet (phase 3)")
    return Out({"rows": [{"date": r.trade_date.isoformat(), "fii_net_cr": float(r.fii_net or 0),
                          "dii_net_cr": float(r.dii_net or 0)} for r in rows]}, "flows", rows[0].source)


@tool("macro")
def get_macro_series(series_id: str, from_date: str = "", to_date: str = "", as_of: str = "") -> Out:
    """Macro observations (BRENT, USDINR, US10Y, GOLD, ...)."""
    ids = [x.strip().upper() for x in series_id.split(",") if x.strip()]
    with ctx().factory() as s:
        rows = s.scalars(select(m.MacroSeries).where(m.MacroSeries.series_id.in_(ids),
                                                     m.MacroSeries.obs_date <= decision_date(as_of))
                         .order_by(m.MacroSeries.obs_date.desc()).limit(200)).all()
    if not rows:
        raise ToolFailure("NOT_FOUND", f"macro series {ids} not ingested yet (phase 3)")
    return Out({"rows": [{"series": r.series_id, "date": r.obs_date.isoformat(), "value": float(r.value)} for r in rows]},
               "macro", "macro_store")


@tool("macro")
def get_sector_sensitivities(factor: str = "", sector: str = "", status: str = "approved", as_of: str = "") -> Out:
    """Approved macro-factor -> sector sensitivity map rows (the transmission map)."""
    with ctx().factory() as s:
        q = (select(m.SectorSensitivity, m.MacroFactor.code, m.Sector.name)
             .join(m.MacroFactor, m.SectorSensitivity.factor_id == m.MacroFactor.id)
             .join(m.Sector, m.SectorSensitivity.sector_id == m.Sector.id).where(m.SectorSensitivity.status == status))
        if factor:
            q = q.where(m.MacroFactor.code == factor)
        if sector:
            q = q.where(m.Sector.name == sector)
        rows = s.execute(q).all()
    return Out({"rows": [{"id": r.id, "factor": f, "sector": sec, "direction": r.direction,
                          "magnitude": float(r.magnitude), "rationale": r.rationale, "version": r.version}
                         for r, f, sec in rows]}, "calc", "sensitivity_map")
