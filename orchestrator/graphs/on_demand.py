"""On-demand analysis (§4.5.3) and market-driven discovery (feature A3).

analyze:  resolve ─(ambiguous)→ END
                 └→ context ─ score ─(Send per horizon)→ research ─ collect ─ END
discover: context ─ screen ─(Send per idea)→ research ─ collect ─ END

Results are stored as recommendation rows with run_type on_demand/discover.
They are never auto-published and never enter the auto-follow portfolio.
"""
from __future__ import annotations

import operator
from datetime import datetime, timedelta
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .. import agents
from ..config import HORIZONS
from ..deps import Deps
from .candidate import RECOVERABLE, build_candidate_graph, candidate_input
from .daily import screen_horizons


class OnDemandState(TypedDict, total=False):
    run_id: int
    decision_time: str
    mode: str                 # analyze | discover
    query: str
    horizons: list[str]
    constraints: dict
    max_ideas: int
    symbol: Optional[str]
    sector: Optional[str]
    clarification: Optional[str]
    regime: Optional[dict]
    events: list
    inputs: list
    results: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]


def build_on_demand_graph(deps: Deps):
    candidate_graph = build_candidate_graph(deps)

    def dt(s: OnDemandState) -> datetime:
        return datetime.fromisoformat(s["decision_time"])

    async def resolve(s: OnDemandState) -> dict:
        r = await deps.tools.call("orchestrator", "resolve_instrument", {"query": s["query"]},
                                  run_id=s["run_id"], as_of=dt(s))
        cands = (r.data or {}).get("candidates", []) if r.ok else []
        if not cands:
            return {"clarification": f"I couldn't find an NSE/BSE stock matching '{s['query']}'."}
        top = cands[0]
        exact = top["symbol"].upper() == s["query"].strip().upper()
        if not exact and (len(cands) > 1 and top.get("confidence", 0) < 0.8):
            names = ", ".join(f"{c['symbol']} ({c['name']})" for c in cands[:5])
            return {"clarification": f"Which one did you mean: {names}?"}
        return {"symbol": top["symbol"], "sector": top.get("sector")}

    def after_resolve(s: OnDemandState) -> str:
        return END if s.get("clarification") else "context"

    async def context(s: OnDemandState) -> dict:
        regime = deps.store.latest_regime()
        errors = []
        if regime is None or regime.get("as_of") != dt(s).date().isoformat():
            try:
                regime, _ = await agents.regime(deps, s["run_id"], dt(s))
                deps.store.save_regime(regime["as_of"], regime)
            except RECOVERABLE as e:
                errors.append(f"regime: {e}")
                regime = regime or {"regime": "unknown"}
        events = deps.store.active_events(since=dt(s) - timedelta(days=deps.cfg.news_lookback_days))
        return {"regime": regime, "events": events, "errors": errors}

    async def score(s: OnDemandState) -> dict:
        inputs, errors = [], []
        for h in s["horizons"]:
            q = await deps.tools.call("orchestrator", "score_horizon",
                                      {"symbol": s["symbol"], "horizon": h, "as_of": s["decision_time"]},
                                      run_id=s["run_id"], as_of=dt(s))
            lv = await deps.tools.call("orchestrator", "propose_levels",
                                       {"symbol": s["symbol"], "horizon": h, "as_of": s["decision_time"]},
                                       run_id=s["run_id"], as_of=dt(s))
            if not (q.ok and lv.ok and q.data and lv.data):
                failed = q if not q.ok else lv
                errors.append(f"{h}: {failed.error.message if failed.error else 'no data'}")
                continue
            inputs.append(candidate_input(run_id=s["run_id"], decision_time=dt(s), symbol=s["symbol"], horizon=h,
                                          sector=s.get("sector"), quant=q.data, levels=lv.data,
                                          regime=s.get("regime") or {}, active_events=s.get("events", []),
                                          run_type="on_demand"))
        return {"inputs": inputs, "errors": errors}

    async def screen(s: OnDemandState) -> dict:
        cands = await screen_horizons(deps, s["run_id"], dt(s), s["horizons"], s.get("regime") or {}, None,
                                      top_k=s.get("max_ideas", 5), constraints=s.get("constraints") or {})
        inputs = [candidate_input(run_id=s["run_id"], decision_time=dt(s), symbol=c["symbol"], horizon=h,
                                  sector=c.get("sector"), quant={k: v for k, v in c.items() if k != "levels"},
                                  levels=c["levels"], regime=s.get("regime") or {},
                                  active_events=s.get("events", []), run_type="discover")
                  for h, rows in cands.items() for c in rows]
        return {"inputs": inputs}

    def after_context(s: OnDemandState) -> str:
        return "screen" if s.get("mode") == "discover" else "score"

    def fan_out(s: OnDemandState):
        return [Send("research", i) for i in s.get("inputs", [])] or "collect"

    async def research(payload: dict) -> dict:
        out = await candidate_graph.ainvoke(payload)
        return {"results": [out["result"]]}

    def collect(s: OnDemandState) -> dict:
        return {}

    g = StateGraph(OnDemandState)
    for name, fn in [("resolve", resolve), ("context", context), ("score", score), ("screen", screen),
                     ("research", research), ("collect", collect)]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, lambda s: "context" if s.get("mode") == "discover" else "resolve",
                            ["resolve", "context"])
    g.add_conditional_edges("resolve", after_resolve, ["context", END])
    g.add_conditional_edges("context", after_context, ["score", "screen"])
    g.add_conditional_edges("score", fan_out, ["research", "collect"])
    g.add_conditional_edges("screen", fan_out, ["research", "collect"])
    g.add_edge("research", "collect")
    g.add_edge("collect", END)
    return g.compile()


async def analyze(deps: Deps, query: str, decision_time: datetime,
                  horizons: Optional[list[str]] = None) -> dict[str, Any]:
    horizons = horizons or list(HORIZONS)
    run_id = deps.store.create_run("on_demand", decision_time, f"analyze:{query}:{','.join(horizons)}",
                                   deps.cfg.version, deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    out = await build_on_demand_graph(deps).ainvoke({
        "run_id": run_id, "decision_time": decision_time.isoformat(), "mode": "analyze", "query": query,
        "horizons": horizons, "results": [], "errors": []})
    _finish(deps, run_id, out)
    return out


async def discover(deps: Deps, decision_time: datetime, horizon: Optional[str] = None,
                   constraints: Optional[dict] = None, max_ideas: int = 5) -> dict[str, Any]:
    run_id = deps.store.create_run("on_demand", decision_time, "user:discover", deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    out = await build_on_demand_graph(deps).ainvoke({
        "run_id": run_id, "decision_time": decision_time.isoformat(), "mode": "discover", "query": "",
        "horizons": [horizon] if horizon else ["short", "medium"], "constraints": constraints or {},
        "max_ideas": max_ideas, "results": [], "errors": []})
    _finish(deps, run_id, out)
    return out


def _finish(deps: Deps, run_id: int, out: dict) -> None:
    status = "succeeded" if out.get("results") or out.get("clarification") else "degraded"
    deps.store.finish_run(run_id, status, deps.budget.run_spent,
                          {"errors": out.get("errors")} if out.get("errors") else None)
