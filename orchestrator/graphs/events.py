"""Event intelligence graphs (§4.2 07:15 event_digest, §4.5 event-triggered run, J3).

event digest:     fetch_news ─ extract ─ store_events ─ impacts ─ END
event-triggered:  load ─ rescore ─(Send per open reco)→ research ─ compare ─ END

Event-triggered re-evaluation runs in shadow mode: it never publishes, it only
raises review tasks for a human (§8.3 review_thesis_break / high_severity_event).
"""
from __future__ import annotations

import asyncio
import operator
from datetime import datetime, timedelta
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .. import agents
from ..deps import Deps
from ..schemas import EventImpacts
from .candidate import RECOVERABLE, build_candidate_graph, candidate_input

BATCH = 20


class EventDigestState(TypedDict, total=False):
    run_id: int
    decision_time: str
    articles: list
    extracted: list
    new_events: list
    triggers: list
    errors: Annotated[list, operator.add]


def check_impacts(out: EventImpacts, approved_ids: set[int]) -> tuple[EventImpacts, list[str]]:
    """§7.2: approved_map rows must reference real map IDs; llm_inference rows are capped."""
    notes, kept = [], []
    for imp in out.impacts:
        if imp.basis == "approved_map":
            real = [i for i in imp.map_row_ids if i in approved_ids]
            if not real:
                notes.append(f"{imp.sector}: approved_map without a real map row, demoted to llm_inference")
                imp.basis = "llm_inference"
            imp.map_row_ids = real
        if imp.basis == "llm_inference":
            if imp.magnitude > 0.4 or imp.confidence > 0.5:
                notes.append(f"{imp.sector}: llm_inference capped")
            imp.magnitude = min(imp.magnitude, 0.4)
            imp.confidence = min(imp.confidence, 0.5)
        kept.append(imp)
    out.impacts = kept
    return out, notes


def build_event_digest_graph(deps: Deps):
    cfg = deps.cfg

    def dt(s) -> datetime:
        return datetime.fromisoformat(s["decision_time"])

    async def fetch_news(s: EventDigestState) -> dict:
        """World news stored by the overnight ingest (deduplicated); live search only if the store is empty."""
        args = {"from_date": (dt(s) - timedelta(days=1)).date().isoformat(), "to_date": dt(s).date().isoformat(),
                "limit": 100}
        r = await deps.tools.call("event_extraction", "get_world_news", args, run_id=s["run_id"], as_of=dt(s))
        items = (r.data or {}).get("items", []) if r.ok else []
        if items:
            return {"articles": items, "errors": []}
        r = await deps.tools.call("event_extraction", "search_news",
                                  {"query": "markets OR economy OR policy OR geopolitics OR commodities",
                                   "from_date": args["from_date"], "to_date": args["to_date"], "limit": 20},
                                  run_id=s["run_id"], as_of=dt(s))
        items = (r.data or {}).get("items", []) if r.ok else []
        return {"articles": items, "errors": [] if r.ok else [f"news: {r.error.code if r.error else '?'}"]}

    async def extract(s: EventDigestState) -> dict:
        batches = [s["articles"][i:i + BATCH] for i in range(0, len(s.get("articles", [])), BATCH)]
        outs = await asyncio.gather(*(agents.extract_events(deps, s["run_id"], dt(s), b) for b in batches),
                                    return_exceptions=True)
        extracted, errors = [], []
        for b, o in zip(batches, outs):
            if isinstance(o, BaseException):
                errors.append(f"extract: {o}")
                continue
            ids = {a["news_id"] for a in b}
            for e in o.events:
                e.source_news_ids = [i for i in e.source_news_ids if i in ids]
                if not e.source_news_ids or e.market_relevance_to_india == "none":
                    continue
                if e.security_flag:
                    deps.store.record_security_event({"run_id": s["run_id"], "agent": "event_extraction",
                                                      "source": "news_batch", "flag": e.security_flag,
                                                      "action_taken": "stripped"})
                extracted.append(e.model_dump(mode="json"))
        return {"extracted": extracted, "errors": errors}

    def store_events(s: EventDigestState) -> dict:
        new = []
        for e in s.get("extracted", []):
            if deps.store.event_exists(e["event_key"]):
                continue
            verified = len(set(e["source_news_ids"])) >= 2
            row = {**e, "status": "active" if verified or e["severity"] < cfg.high_severity_threshold else "unverified",
                   "first_seen": s["decision_time"], "extracted_by_run": s["run_id"]}
            row["id"] = deps.store.save_event(row)
            if e["severity"] >= cfg.high_severity_threshold:
                deps.store.create_hitl_task(
                    "high_severity_event", "event", row["id"], payload=row,
                    ai_recommendation="confirm" if verified else "verify sources",
                    reason=f"severity {e['severity']}" + ("" if verified else ", single source"),
                    priority=1, default_on_expiry="shadow_run")
            new.append(row)
        return {"new_events": new}

    async def impacts(s: EventDigestState) -> dict:
        targets = [e for e in s.get("new_events", []) if e["market_relevance_to_india"] in ("medium", "high")]
        outs = await asyncio.gather(*(agents.event_impact(deps, s["run_id"], dt(s), e) for e in targets),
                                    return_exceptions=True)
        errors, triggers = [], []
        for e, o in zip(targets, outs):
            if isinstance(o, BaseException):
                errors.append(f"impact {e['event_key']}: {o}")
                continue
            out, ev = o
            approved = {row["id"] for i in ev.items if i.tool == "get_sector_sensitivities"
                        for row in (i.data or {}).get("rows", [])}
            out.event_id = e["id"]
            out, notes = check_impacts(out, approved)
            deps.store.save_event_impacts(e["id"], {**out.model_dump(mode="json"), "notes": notes}, s["run_id"])
            if e["severity"] >= cfg.event_trigger_severity and e["status"] == "active":
                triggers.append(e["id"])
        deps.store.finish_run(s["run_id"], "degraded" if errors or s.get("errors") else "succeeded",
                              deps.budget.run_spent)
        return {"triggers": triggers, "errors": errors}

    g = StateGraph(EventDigestState)
    for name, fn in [("fetch_news", fetch_news), ("extract", extract), ("store_events", store_events),
                     ("impacts", impacts)]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch_news")
    g.add_edge("fetch_news", "extract")
    g.add_edge("extract", "store_events")
    g.add_edge("store_events", "impacts")
    g.add_edge("impacts", END)
    return g.compile()


class EventTriggeredState(TypedDict, total=False):
    run_id: int
    decision_time: str
    event_ids: list[int]
    events: list
    affected: list
    inputs: list
    results: Annotated[list, operator.add]
    tasks: list
    errors: Annotated[list, operator.add]


def build_event_triggered_graph(deps: Deps, min_magnitude: float = 0.3):
    candidate_graph = build_candidate_graph(deps)

    def dt(s) -> datetime:
        return datetime.fromisoformat(s["decision_time"])

    def load(s: EventTriggeredState) -> dict:
        events = [e for e in deps.store.active_events(since=dt(s) - timedelta(days=30)) if e["id"] in s["event_ids"]]
        sectors = {i["sector"] for e in events for i in e.get("impacts", []) if i["magnitude"] >= min_magnitude}
        return {"events": events, "affected": deps.store.open_recos(sectors) if sectors else []}

    async def rescore(s: EventTriggeredState) -> dict:
        regime = deps.store.latest_regime() or {}
        inputs = []
        for reco in s.get("affected", []):
            args = {"symbol": reco["symbol"], "horizon": reco["horizon"], "as_of": s["decision_time"]}
            q = await deps.tools.call("orchestrator", "score_horizon", args, run_id=s["run_id"], as_of=dt(s))
            lv = await deps.tools.call("orchestrator", "propose_levels", args, run_id=s["run_id"], as_of=dt(s))
            if q.ok and lv.ok:
                inputs.append({**candidate_input(run_id=s["run_id"], decision_time=dt(s), symbol=reco["symbol"],
                                                 horizon=reco["horizon"], sector=reco.get("sector"), quant=q.data,
                                                 levels=lv.data, regime=regime, active_events=s["events"],
                                                 run_type="event_triggered")})
        return {"inputs": inputs}

    def fan_out(s: EventTriggeredState):
        return [Send("research", i) for i in s.get("inputs", [])] or "compare"

    async def research(payload: dict) -> dict:
        try:
            out = await candidate_graph.ainvoke(payload)
        except RECOVERABLE as e:
            return {"errors": [f"research {payload['symbol']}: {e}"]}
        return {"results": [out["result"]]}

    def compare(s: EventTriggeredState) -> dict:
        old = {(r["symbol"], r["horizon"]): r for r in s.get("affected", [])}
        tasks = []
        for r in s.get("results", []):
            prev = old.get((r["symbol"], r["horizon"]))
            if not prev:
                continue
            changed = r["verdict"] != prev["verdict"] or r["status"] == "rejected"
            if changed:
                tid = deps.store.create_hitl_task(
                    "review_thesis_break", "recommendation", prev["id"],
                    payload={"previous": prev, "reevaluation": r, "event_ids": s["event_ids"]},
                    ai_recommendation="close" if r["verdict"] == "avoid" else "review",
                    reason=f"event re-evaluation: {prev['verdict']} -> {r['verdict']} ({r['status']})",
                    priority=1, default_on_expiry="keep_and_remind")
                tasks.append(tid)
        deps.store.finish_run(s["run_id"], "succeeded", deps.budget.run_spent)
        return {"tasks": tasks}

    g = StateGraph(EventTriggeredState)
    for name, fn in [("load", load), ("rescore", rescore), ("research", research), ("compare", compare)]:
        g.add_node(name, fn)
    g.add_edge(START, "load")
    g.add_edge("load", "rescore")
    g.add_conditional_edges("rescore", fan_out, ["research", "compare"])
    g.add_edge("research", "compare")
    g.add_edge("compare", END)
    return g.compile()


async def run_event_digest(deps: Deps, decision_time: datetime) -> dict[str, Any]:
    run_id = deps.store.create_run("event_digest", decision_time, "schedule", deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    return await build_event_digest_graph(deps).ainvoke(
        {"run_id": run_id, "decision_time": decision_time.isoformat(), "errors": []})


async def run_event_triggered(deps: Deps, decision_time: datetime, event_ids: list[int]) -> dict[str, Any]:
    run_id = deps.store.create_run("event_triggered", decision_time, f"events:{event_ids}", deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    return await build_event_triggered_graph(deps).ainvoke(
        {"run_id": run_id, "decision_time": decision_time.isoformat(), "event_ids": event_ids,
         "results": [], "errors": []})
