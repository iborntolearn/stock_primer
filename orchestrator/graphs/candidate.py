"""Per-stock research subgraph (§4.5.2 "candidate research to publication").

START ─┬─ technical ──┐
       ├─ fundamental ┼─ combine ─ critic ─ synthesize ─ validate ─ record ─ END
       └─ news ───────┘

Used by the daily run (fan-out via Send), on-demand analysis, discovery and
event-triggered re-evaluation. A failing or timed-out analyst degrades
confidence instead of failing the stock (§4.5.2, §6.6).
"""
from __future__ import annotations

import asyncio
import logging
import operator
from datetime import datetime
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import agents
from ..budget import BudgetExceeded
from ..deps import Deps
from ..evidence import EvidenceBundle
from ..llm import LLMCallFailed
from ..ports import ToolForbidden
from ..schemas import RecommendationCard
from ..scoring import combine
from ..validation import validate_card

log = logging.getLogger(__name__)

RECOVERABLE = (LLMCallFailed, asyncio.TimeoutError, ToolForbidden, BudgetExceeded, RuntimeError)


def _merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class CandidateState(TypedDict, total=False):
    # inputs
    run_id: int
    decision_time: str
    symbol: str
    horizon: str
    sector: Optional[str]
    quant: dict            # score_horizon output: quant_score, confidence, gates, gates_passed, rank, above_200dma, atr
    levels: dict           # propose_levels output: entry_low, entry_high, stop, target, rr, method
    regime: dict
    active_events: list
    run_type: str          # daily | on_demand | discover | event_triggered
    # accumulated
    views: Annotated[dict, _merge]
    evidence: Annotated[list, operator.add]
    gaps: Annotated[list, operator.add]
    scored: dict
    critique: Optional[dict]
    card: Optional[dict]
    violations: list
    result: dict


def build_candidate_graph(deps: Deps):
    cfg = deps.cfg

    def dt(s: CandidateState) -> datetime:
        return datetime.fromisoformat(s["decision_time"])

    async def _analyst(name: str, s: CandidateState, coro) -> dict:
        try:
            view, ev = await asyncio.wait_for(coro, timeout=cfg.node_timeout_s)
        except RECOVERABLE as e:
            log.warning("%s %s/%s failed: %s", name, s["symbol"], s["horizon"], e)
            return {"views": {name: None}, "gaps": [f"{name}: {type(e).__name__}"]}
        if view and view.get("security_flag"):
            deps.store.record_security_event({"run_id": s["run_id"], "agent": name, "source": s["symbol"],
                                              "flag": view["security_flag"], "action_taken": "stripped"})
        if view:
            deps.store.save_agent_output(s["run_id"], name, "instrument", s["symbol"], view, "passed",
                                         view.get("confidence"))
        return {"views": {name: view}, "evidence": ev.to_state(), "gaps": ev.gaps}

    async def technical(s: CandidateState) -> dict:
        return await _analyst("technical", s, agents.technical(deps, s["run_id"], dt(s), s["symbol"], s["horizon"], s["quant"]))

    async def fundamental(s: CandidateState) -> dict:
        return await _analyst("fundamental", s, agents.fundamental(deps, s["run_id"], dt(s), s["symbol"], s["horizon"]))

    async def news(s: CandidateState) -> dict:
        return await _analyst("news", s, agents.news(deps, s["run_id"], dt(s), s["symbol"], s["horizon"],
                                                     s.get("active_events", [])))

    def _quant_block(s: CandidateState, scored: dict) -> dict:
        return {"symbol": s["symbol"], "horizon": s["horizon"], **s["quant"], "levels": s["levels"],
                "agent_adjustment": scored["agent_adjustment"], "final_score": scored["final_score"],
                "verdict": scored["verdict"], "max_confidence": scored["max_confidence"],
                "contributions": scored["contributions"]}

    def combine_node(s: CandidateState) -> dict:
        timeouts = sum(1 for g in s.get("gaps", []) if g.split(":")[0] in ("technical", "fundamental", "news"))
        scored = combine(s["quant"], s.get("views", {}), None, cfg, extra_gaps=timeouts)
        return {"scored": scored}

    def after_combine(s: CandidateState) -> str:
        views = s.get("views", {})
        return "critic" if any(views.get(k) for k in ("technical", "news", "fundamental")) else "record"

    async def critic_node(s: CandidateState) -> dict:
        ev = EvidenceBundle.from_state(s.get("evidence", []))
        try:
            critique = await asyncio.wait_for(
                agents.critic(deps, s["run_id"], dt(s), s["symbol"], s["horizon"], s.get("views", {}),
                              _quant_block(s, s["scored"]), ev),
                timeout=cfg.node_timeout_s)
        except RECOVERABLE as e:
            # No critic, no publication: force human review downstream.
            return {"critique": None, "gaps": [f"critic: {type(e).__name__}"]}
        timeouts = sum(1 for g in s.get("gaps", []) if g.split(":")[0] in ("technical", "fundamental", "news"))
        scored = combine(s["quant"], s.get("views", {}), critique, cfg, extra_gaps=timeouts)
        deps.store.save_agent_output(s["run_id"], "critic", "instrument", s["symbol"], critique, "passed", None)
        return {"critique": critique, "scored": scored}

    async def synthesize_node(s: CandidateState) -> dict:
        ev = EvidenceBundle.from_state(s.get("evidence", []))
        try:
            card = await asyncio.wait_for(
                agents.synthesize(deps, s["run_id"], dt(s), s.get("views", {}), s.get("critique"),
                                  _quant_block(s, s["scored"]), s.get("regime", {}), ev),
                timeout=cfg.node_timeout_s)
        except RECOVERABLE as e:
            return {"card": None, "gaps": [f"synthesizer: {type(e).__name__}"]}
        return {"card": card.model_dump(mode="json")}

    def validate_node(s: CandidateState) -> dict:
        if not s.get("card"):
            return {"violations": [{"code": "V1", "message": "no card produced", "action": "rejected"}]}
        ev = EvidenceBundle.from_state(s.get("evidence", []))
        rep = validate_card(RecommendationCard.model_validate(s["card"]), quant=s["quant"], levels=s["levels"],
                            scored=s["scored"], critique=s.get("critique"), views=s.get("views", {}),
                            evidence=ev.by_ref(), decision_time=dt(s), cfg=cfg)
        violations = rep.summary()
        if s.get("critique") is None:
            violations.append({"code": "V8", "message": "critic unavailable", "action": "hitl"})
        return {"card": rep.card.model_dump(mode="json"), "violations": violations}

    def record_node(s: CandidateState) -> dict:
        violations = s.get("violations", [])
        actions = {v["action"] for v in violations}
        card = s.get("card")
        if not card:
            status = "rejected"
        elif "rejected" in actions:
            status = "rejected"
        elif "hitl" in actions:
            status = "hitl_pending"
        else:
            status = "validated"
        scored = s.get("scored") or combine(s["quant"], {}, None, cfg)
        reco = {
            "run_id": s["run_id"], "symbol": s["symbol"], "sector": s.get("sector"), "horizon": s["horizon"],
            "verdict": scored["verdict"], "action": card["action"] if card else None,
            "quant_score": scored["quant_score"], "agent_adjustment": scored["agent_adjustment"],
            "final_score": scored["final_score"], "confidence": card["confidence"] if card else 0.0,
            **{k: s["levels"].get(k) for k in ("entry_low", "entry_high", "stop", "target")},
            "risk_reward": s["levels"].get("rr"), "thesis": card["thesis"] if card else "",
            "bull_case": card["bull_case"] if card else [], "bear_case": card["bear_case"] if card else [],
            "key_risks": card["key_risks"] if card else [], "invalidation": card["invalidation"] if card else [],
            "regime_at_issue": (s.get("regime") or {}).get("regime"), "card": card or {},
            "status": status, "run_type": s.get("run_type", "daily"),
            "evidence_ids": sorted({e for c in (card or {}).get("claims", []) for e in c["evidence_ids"]}),
            "violations": violations, "gaps": s.get("gaps", []), "critique": s.get("critique"),
            "contributions": scored.get("contributions", []),
            "strategy_version": cfg.strategy_version, "prompt_bundle_version": cfg.prompt_bundle_version,
        }
        reco_id = deps.store.save_recommendation(reco)
        return {"result": {**{k: reco[k] for k in ("symbol", "sector", "horizon", "verdict", "final_score",
                                                    "confidence", "status", "violations", "gaps", "card")},
                           "reco_id": reco_id}}

    g = StateGraph(CandidateState)
    g.add_node("technical", technical)
    g.add_node("fundamental", fundamental)
    g.add_node("news", news)
    g.add_node("combine", combine_node)
    g.add_node("critic", critic_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("validate", validate_node)
    g.add_node("record", record_node)
    for n in ("technical", "fundamental", "news"):
        g.add_edge(START, n)
    g.add_edge(["technical", "fundamental", "news"], "combine")
    g.add_conditional_edges("combine", after_combine, ["critic", "record"])
    g.add_edge("critic", "synthesize")
    g.add_edge("synthesize", "validate")
    g.add_edge("validate", "record")
    g.add_edge("record", END)
    return g.compile()


def candidate_input(*, run_id: int, decision_time: datetime, symbol: str, horizon: str, quant: dict, levels: dict,
                    regime: dict, active_events: list, run_type: str, sector: Optional[str] = None) -> dict[str, Any]:
    return {"run_id": run_id, "decision_time": decision_time.isoformat(), "symbol": symbol, "horizon": horizon,
            "sector": sector, "quant": quant, "levels": levels, "regime": regime,
            "active_events": active_events, "run_type": run_type, "views": {}, "evidence": [], "gaps": []}
