"""The 12 agents (§4.5.1) plus the single-shot LLM tasks the orchestrator uses.

Pattern for research agents: the orchestrator gathers evidence through the
agent's allowlisted tools (§5.6), then makes one structured LLM call whose
prompt contains only that evidence. This keeps runs reproducible and cheap.
The Q&A agent is the exception: it runs a real tool loop.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

from .deps import Deps
from .evidence import EvidenceBundle, escape_text, as_json, gather
from .schemas import (
    CritiqueReport, Digest, EventImpacts, EventList, FundamentalView, NewsView, PortfolioReview,
    RecommendationCard, RegimeAssessment, RouteDecision, SectorView, TechnicalView, TriageResult,
)


def _d(dt: datetime, days: int = 0) -> str:
    return (dt - timedelta(days=days)).date().isoformat()


# -- 1. Query Router (LLM-01) ---------------------------------------------------
async def route_query(deps: Deps, run_id: int, dt: datetime, message: str, history: list[str]) -> RouteDecision:
    return await deps.llm.structured(
        call_id="LLM-01", agent="router", run_id=run_id, decision_time=dt, output_model=RouteDecision,
        sections={"message": message, "session_context": "\n".join(history[-3:]) or "(none)"},
    )


# -- 2. Market Regime (LLM-04) --------------------------------------------------
async def regime(deps: Deps, run_id: int, dt: datetime) -> tuple[dict, EvidenceBundle]:
    """Returns the regime snapshot. The label is the quant label; the LLM adds narrative only."""
    ev = await gather(deps.tools, "regime", [
        ("get_regime_inputs", {}),
        ("get_market_breadth", {}),
        ("get_flows", {"from_date": _d(dt, 10), "to_date": _d(dt)}),
    ], run_id=run_id, as_of=dt)
    inputs = next((i.data for i in ev.items if i.tool == "get_regime_inputs"), None)
    if not inputs or "quant_label" not in inputs:
        raise RuntimeError("regime inputs unavailable")
    label = inputs["quant_label"]
    assessment = await deps.llm.structured(
        call_id="LLM-04", agent="regime", run_id=run_id, decision_time=dt, output_model=RegimeAssessment,
        sections={"quant_label": label, "inputs": ev.render()}, cite_ids=ev.ids(),
    )
    snap = {"as_of": _d(dt), "regime": label, "score": inputs.get("score"), "inputs": inputs,
            "assessment": assessment.model_dump(mode="json")}
    deps.store.save_agent_output(run_id, "regime", "market", "market", snap["assessment"], "passed", None)
    return snap, ev


# -- 3. Event Extraction (LLM-02) -----------------------------------------------
async def extract_events(deps: Deps, run_id: int, dt: datetime, articles: list[dict]) -> EventList:
    docs = "\n".join(
        f'<untrusted_document id="N{a["news_id"]}" source="{a.get("source", "")}" published="{a.get("published_at", "")}">'
        f'{escape_text(a.get("title", ""))} — {escape_text(a.get("snippet", ""))}</untrusted_document>'
        for a in articles)
    return await deps.llm.structured(
        call_id="LLM-02", agent="event_extraction", run_id=run_id, decision_time=dt, output_model=EventList,
        sections={"articles": docs},
    )


# -- 4. Event Impact (LLM-03) ---------------------------------------------------
async def event_impact(deps: Deps, run_id: int, dt: datetime, event: dict) -> tuple[EventImpacts, EvidenceBundle]:
    ev = await gather(deps.tools, "event_impact", [
        ("get_sector_sensitivities", {"status": "approved"}),
        ("get_macro_series", {"series_id": "BRENT,USDINR,US10Y,GOLD", "from_date": _d(dt, 10), "to_date": _d(dt)}),
        ("get_index_history", {"index_code": "SECTOR_INDICES", "from_date": _d(dt, 10), "to_date": _d(dt)}),
    ], run_id=run_id, as_of=dt)
    out = await deps.llm.structured(
        call_id="LLM-03", agent="event_impact", run_id=run_id, decision_time=dt, output_model=EventImpacts,
        sections={"event": as_json(event), "sensitivity_map": ev.render({"get_sector_sensitivities"}),
                  "macro_moves": ev.render({"get_macro_series", "get_index_history"})},
        cite_ids=ev.ids(),
    )
    return out, ev


# -- 5. Sector/Theme (LLM-05) ---------------------------------------------------
async def sector_view(deps: Deps, run_id: int, dt: datetime, regime_snap: dict,
                      impacts: list[dict]) -> tuple[SectorView, EvidenceBundle]:
    ev = await gather(deps.tools, "sector", [
        ("get_index_history", {"index_code": "SECTOR_INDICES", "from_date": _d(dt, 130), "to_date": _d(dt)}),
    ], run_id=run_id, as_of=dt)
    out = await deps.llm.structured(
        call_id="LLM-05", agent="sector", run_id=run_id, decision_time=dt, output_model=SectorView,
        sections={"sector_returns": ev.render(), "event_impacts": as_json(impacts),
                  "regime": as_json({"label": regime_snap["regime"], **regime_snap.get("assessment", {})})},
        cite_ids=ev.ids(),
    )
    deps.store.save_agent_output(run_id, "sector", "market", "sectors", out.model_dump(mode="json"), "passed", None)
    return out, ev


# -- News triage (LLM-06, single-shot task) -------------------------------------
async def triage(deps: Deps, run_id: int, dt: datetime, symbols: list[str]) -> TriageResult:
    bundles = await asyncio.gather(*(gather(deps.tools, "orchestrator", [
        ("get_stock_news", {"symbol": s, "from_date": _d(dt, 7), "to_date": _d(dt), "limit": 8})
    ], run_id=run_id, as_of=dt) for s in symbols))
    blocks = "\n".join(f'<stock symbol="{s}">{b.render()}</stock>' for s, b in zip(symbols, bundles))
    return await deps.llm.structured(
        call_id="LLM-06", agent="triage", run_id=run_id, decision_time=dt, output_model=TriageResult,
        sections={"candidates": blocks},
    )


# -- 6. Technical Analyst (LLM-07) ----------------------------------------------
async def technical(deps: Deps, run_id: int, dt: datetime, symbol: str, horizon: str,
                    quant: dict) -> tuple[Optional[dict], EvidenceBundle]:
    lookback = {"short": 120, "medium": 300, "long": 400}[horizon]
    ev = await gather(deps.tools, "technical", [
        ("compute_indicators", {"symbol": symbol, "indicators": ["RSI14", "ATR14", "SMA20", "SMA50", "SMA200",
                                                                 "VOL_RATIO20", "BREAKOUT20", "BREAKOUT55", "HI52", "LO52"]}),
        ("get_price_history", {"symbol": symbol, "from_date": _d(dt, lookback), "to_date": _d(dt), "adjusted": True}),
        ("get_index_history", {"index_code": quant.get("sector_index", "NIFTY500"), "from_date": _d(dt, lookback), "to_date": _d(dt)}),
    ], run_id=run_id, as_of=dt)
    out = await deps.llm.structured(
        call_id="LLM-07", agent="technical", run_id=run_id, decision_time=dt, output_model=TechnicalView,
        sections={"indicators": ev.render(), "quant_score": as_json(quant)}, cite_ids=ev.ids(),
        template_vars={"symbol": symbol, "horizon": horizon},
    )
    return out.model_dump(mode="json"), ev


# -- 7. Fundamental Analyst (LLM-08) --------------------------------------------
async def fundamental(deps: Deps, run_id: int, dt: datetime, symbol: str,
                      horizon: str) -> tuple[Optional[dict], EvidenceBundle]:
    if horizon == "short":
        return None, EvidenceBundle()   # §6.2: medium/long only
    ev = await gather(deps.tools, "fundamental", [
        ("get_ratios", {"symbol": symbol}),
        ("get_valuation", {"symbol": symbol}),
        ("get_financials", {"symbol": symbol, "period_type": "Q", "n_periods": 8}),
        ("get_shareholding", {"symbol": symbol, "n_quarters": 6}),
        ("search_filings", {"symbol": symbol, "query": "auditor remarks related party pledge guidance",
                            "from_date": _d(dt, 365), "to_date": _d(dt)}),
    ], run_id=run_id, as_of=dt)
    if not any(i.tool in ("get_ratios", "get_financials") for i in ev.items):
        # No fundamentals at all (price-led phase): record the gap instead of paying for an empty analysis.
        ev.gaps.append("fundamental: no fundamentals data available")
        return None, ev
    out = await deps.llm.structured(
        call_id="LLM-08", agent="fundamental", run_id=run_id, decision_time=dt, output_model=FundamentalView,
        sections={"ratios": ev.render({"get_ratios", "get_valuation", "get_shareholding"}),
                  "statements": ev.render({"get_financials"}),
                  "filing_excerpts": ev.render({"search_filings"})},
        cite_ids=ev.ids(), template_vars={"symbol": symbol, "horizon": horizon},
    )
    return out.model_dump(mode="json"), ev


# -- 8. News/Filings Analyst (LLM-09) -------------------------------------------
async def news(deps: Deps, run_id: int, dt: datetime, symbol: str, horizon: str,
               active_events: list[dict]) -> tuple[Optional[dict], EvidenceBundle]:
    days = deps.cfg.news_lookback_days
    window = {"short": 30, "medium": 120, "long": 365}[horizon]
    ev = await gather(deps.tools, "news", [
        ("get_stock_news", {"symbol": symbol, "from_date": _d(dt, days), "to_date": _d(dt), "limit": 12}),
        ("get_event_calendar", {"symbol": symbol, "from_date": _d(dt), "to_date": (dt + timedelta(days=window)).date().isoformat()}),
        ("get_corporate_actions", {"symbol": symbol, "from_date": _d(dt, 30), "to_date": (dt + timedelta(days=window)).date().isoformat(),
                                   "types": ["split", "bonus", "dividend", "rights", "buyback", "merger"]}),
    ], run_id=run_id, as_of=dt)
    out = await deps.llm.structured(
        call_id="LLM-09", agent="news", run_id=run_id, decision_time=dt, output_model=NewsView,
        sections={"news": ev.render({"get_stock_news"}),
                  "calendar": ev.render({"get_event_calendar", "get_corporate_actions"}),
                  "active_events": as_json(active_events[:10])},
        cite_ids=ev.ids(), template_vars={"symbol": symbol, "lookback_days": str(days)},
    )
    return out.model_dump(mode="json"), ev


# -- 9. Critic / Red Team (LLM-10) ----------------------------------------------
async def critic(deps: Deps, run_id: int, dt: datetime, symbol: str, horizon: str, views: dict[str, Any],
                 quant_block: dict, ev: EvidenceBundle) -> dict:
    calendar = ev.render({"get_event_calendar", "get_corporate_actions"})
    out = await deps.llm.structured(
        call_id="LLM-10", agent="critic", run_id=run_id, decision_time=dt, output_model=CritiqueReport,
        sections={"views": as_json(views), "quant": as_json(quant_block), "calendar": calendar,
                  "evidence": ev.render()},
        cite_ids=ev.ids(),
    )
    return out.model_dump(mode="json")


# -- 10. Synthesizer (LLM-11) ---------------------------------------------------
async def synthesize(deps: Deps, run_id: int, dt: datetime, views: dict[str, Any], critique: Optional[dict],
                     quant_block: dict, regime_snap: dict, ev: EvidenceBundle) -> RecommendationCard:
    return await deps.llm.structured(
        call_id="LLM-11", agent="synthesizer", run_id=run_id, decision_time=dt, output_model=RecommendationCard,
        sections={"quant": as_json(quant_block), "views": as_json(views), "critique": as_json(critique or {}),
                  "regime": as_json({"label": regime_snap.get("regime"),
                                     "narrative": regime_snap.get("assessment", {}).get("narrative")}),
                  "evidence": ev.render()},
        cite_ids=ev.ids(),
    )


# -- 11. Portfolio Review (LLM-12) ----------------------------------------------
async def portfolio_review(deps: Deps, run_id: int, dt: datetime, portfolio_id: int,
                           active_events: list[dict]) -> tuple[PortfolioReview, EvidenceBundle]:
    base = await gather(deps.tools, "portfolio_review", [
        ("get_portfolio", {"portfolio_id": portfolio_id}),
        ("get_exposure", {"portfolio_id": portfolio_id}),
    ], run_id=run_id, as_of=dt)
    holdings = next((i.data for i in base.items if i.tool == "get_portfolio"), {}) or {}
    symbols = [p["symbol"] for p in holdings.get("positions", [])]
    per_symbol = await gather(deps.tools, "portfolio_review", [
        call for s in symbols for call in (
            ("get_stock_news", {"symbol": s, "from_date": _d(dt, 14), "to_date": _d(dt), "limit": 6}),
            ("get_price_history", {"symbol": s, "from_date": _d(dt, 30), "to_date": _d(dt)}),
        )
    ], run_id=run_id, as_of=dt)
    ev = base.merge(per_symbol)
    out = await deps.llm.structured(
        call_id="LLM-12", agent="portfolio_review", run_id=run_id, decision_time=dt, output_model=PortfolioReview,
        sections={"positions": base.render(), "updates": per_symbol.render(),
                  "active_events": as_json(active_events[:10])},
        cite_ids=ev.ids(),
    )
    return out, ev


# -- 12. Q&A (LLM-14) -----------------------------------------------------------
async def answer_question(deps: Deps, run_id: int, dt: datetime, question: str) -> tuple[str, list[str]]:
    return await deps.llm.tool_answer(call_id="LLM-14", agent="qa", run_id=run_id, decision_time=dt,
                                      question=question, tools=deps.tools, role="qa")


# -- Digest writer (LLM-16, single-shot task) -----------------------------------
async def write_digest(deps: Deps, run_id: int, dt: datetime, cards: list[dict], regime_snap: Optional[dict],
                       events: list[dict], no_pick_reasons: dict[str, str], attention: list[str]) -> Digest:
    return await deps.llm.structured(
        call_id="LLM-16", agent="digest", run_id=run_id, decision_time=dt, output_model=Digest,
        sections={"cards": as_json(cards), "regime": as_json(regime_snap or {}),
                  "events": as_json(events[:8]), "no_pick_reasons": as_json(no_pick_reasons),
                  "attention_items": as_json(attention)},
    )
