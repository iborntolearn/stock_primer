"""Conversational entry point (Telegram /analyze, web chat): route, then dispatch.

router ─┬─ clarify ──────────────┐
        ├─ analyze  (on-demand)  │
        ├─ discover (on-demand)  ├─ END
        ├─ qa       (tool loop)  │
        └─ unsupported ──────────┘
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import agents
from ..config import HORIZONS
from ..deps import Deps
from .candidate import RECOVERABLE
from .on_demand import build_on_demand_graph

QA_INTENTS = {"explain_pick", "market_context", "portfolio_question", "other"}


class ChatState(TypedDict, total=False):
    run_id: int
    decision_time: str
    message: str
    history: list[str]
    route: Optional[dict]
    reply: str
    citations: list[str]
    results: list


def build_chat_graph(deps: Deps):
    on_demand = build_on_demand_graph(deps)

    def dt(s: ChatState) -> datetime:
        return datetime.fromisoformat(s["decision_time"])

    async def router(s: ChatState) -> dict:
        try:
            r = await agents.route_query(deps, s["run_id"], dt(s), s["message"], s.get("history", []))
        except RECOVERABLE:
            return {"route": {"intent": "other", "symbols": [], "horizon": None, "constraints": {},
                              "needs_clarification": False}}
        return {"route": r.model_dump(mode="json")}

    def dispatch(s: ChatState) -> str:
        r = s["route"]
        if r.get("needs_clarification"):
            return "clarify"
        if r["intent"] == "analyze_stock" and r.get("symbols"):
            return "analyze"
        if r["intent"] == "discover_ideas":
            return "discover"
        if r["intent"] in ("add_to_portfolio", "settings"):
            return "unsupported"
        return "qa"

    def clarify(s: ChatState) -> dict:
        return {"reply": s["route"].get("clarifying_question") or "Could you say which stock or horizon you mean?"}

    def _horizons(r: dict) -> list[str]:
        h = r.get("horizon")
        return list(HORIZONS) if h in (None, "all") else [h]

    async def analyze(s: ChatState) -> dict:
        r = s["route"]
        out = await on_demand.ainvoke({"run_id": s["run_id"], "decision_time": s["decision_time"], "mode": "analyze",
                                       "query": r["symbols"][0], "horizons": _horizons(r), "results": [],
                                       "errors": []})
        if out.get("clarification"):
            return {"reply": out["clarification"], "results": []}
        return {"reply": _summarise(out.get("results", [])), "results": out.get("results", [])}

    async def discover(s: ChatState) -> dict:
        r = s["route"]
        c = r.get("constraints") or {}
        out = await on_demand.ainvoke({"run_id": s["run_id"], "decision_time": s["decision_time"], "mode": "discover",
                                       "query": "", "horizons": _horizons(r) if r.get("horizon") else ["short", "medium"],
                                       "constraints": {"sector": c.get("sector")} if c.get("sector") else {},
                                       "max_ideas": c.get("max_ideas") or 5, "results": [], "errors": []})
        return {"reply": _summarise(out.get("results", [])), "results": out.get("results", [])}

    async def qa(s: ChatState) -> dict:
        try:
            text, cites = await agents.answer_question(deps, s["run_id"], dt(s), s["message"])
        except RECOVERABLE as e:
            return {"reply": f"I couldn't answer that right now ({type(e).__name__}).", "citations": []}
        return {"reply": text, "citations": cites}

    def unsupported(s: ChatState) -> dict:
        intent = s["route"]["intent"]
        msg = ("Use the Add to portfolio button on a card (or the web app) so the order gets an idempotency key."
               if intent == "add_to_portfolio" else "Settings changes go through the Settings screen and need approval.")
        return {"reply": msg}

    g = StateGraph(ChatState)
    for name, fn in [("router", router), ("clarify", clarify), ("analyze", analyze), ("discover", discover),
                     ("qa", qa), ("unsupported", unsupported)]:
        g.add_node(name, fn)
    g.add_edge(START, "router")
    g.add_conditional_edges("router", dispatch, ["clarify", "analyze", "discover", "qa", "unsupported"])
    for n in ("clarify", "analyze", "discover", "qa", "unsupported"):
        g.add_edge(n, END)
    return g.compile()


def _summarise(results: list[dict], errors: list[str] | None = None) -> str:
    if not results:
        why = "; ".join(errors or []) or "data or gates unavailable"
        return f"No analysis could be produced ({why})."
    lines = []
    for r in sorted(results, key=lambda r: ("short", "medium", "long").index(r["horizon"])):
        card = r.get("card") or {}
        head = f"{r['symbol']} {r['horizon']}: {r['verdict']} (confidence {r['confidence']:.2f}, status {r['status']})"
        if card:
            lv = card["levels"]
            head += f"\n  {card['headline']}\n  Entry {lv['entry_low']}-{lv['entry_high']} | Stop {lv['stop']} | Target {lv['target']}"
        elif r.get("violations"):
            head += "\n  " + "; ".join(v["message"] for v in r["violations"][:2])
        lines.append(head)
    lines.append("Decision-support only, not investment advice.")
    return "\n".join(lines)


async def chat(deps: Deps, message: str, decision_time: datetime, history: Optional[list[str]] = None) -> dict[str, Any]:
    run_id = deps.store.create_run("on_demand", decision_time, "chat", deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    out = await build_chat_graph(deps).ainvoke({"run_id": run_id, "decision_time": decision_time.isoformat(),
                                                "message": message, "history": history or []})
    deps.store.finish_run(run_id, "succeeded", deps.budget.run_spent)
    return out
