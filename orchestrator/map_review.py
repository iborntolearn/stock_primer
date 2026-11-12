"""Weekly transmission-map review (feature B2, LLM-13 Map Change Proposer).

The LLM sees the approved map, recent events with their sector impacts, and sector
index moves; it proposes add/modify/retire edits. Deterministic checks then drop
anything malformed or under-evidenced, cap magnitudes, and file each surviving
proposal as a 'proposed' map row plus an approve_map_change review task. Nothing
changes the live map without a human decision.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .deps import Deps
from .evidence import as_json, gather
from .graphs.candidate import RECOVERABLE
from .schemas import MapChangeProposals

MAX_MAGNITUDE = 0.8


def check_proposals(out: MapChangeProposals, *, factors: set[str], sectors: set[str], event_ids: set[int],
                    min_obs: int, current: dict[tuple[str, str], int]) -> tuple[list[dict], list[str]]:
    kept, dropped = [], []
    for p in out.proposals:
        tag = f"{p.action} {p.factor}->{p.sector}"
        if p.factor not in factors or p.sector not in sectors:
            dropped.append(f"{tag}: unknown factor or sector")
            continue
        support = sorted(set(p.supporting_event_ids) & event_ids)
        if len(support) < min_obs:
            dropped.append(f"{tag}: {len(support)} supporting event(s) < {min_obs}")
            continue
        existing = current.get((p.factor, p.sector))
        action = p.action
        if action in ("modify", "retire") and existing is None:
            if action == "retire":
                dropped.append(f"{tag}: nothing to retire")
                continue
            action = "add"
        if action == "add" and existing is not None:
            action = "modify"
        kept.append({"action": action, "factor": p.factor, "sector": p.sector, "direction": p.direction,
                     "magnitude": min(p.magnitude, MAX_MAGNITUDE), "rationale": p.rationale,
                     "supporting_event_ids": support, "expected_confidence": p.expected_confidence,
                     "current_row_id": existing})
    return kept, dropped


async def run_map_review(deps: Deps, decision_time: datetime, min_obs: int = 3, lookback_days: int = 30) -> dict[str, Any]:
    run_id = deps.store.create_run("map_review", decision_time, "schedule", deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    events = [e for e in deps.store.active_events(since=decision_time - timedelta(days=lookback_days)) if e.get("impacts")]
    if len(events) < min_obs:
        deps.store.finish_run(run_id, "succeeded", deps.budget.run_spent)
        return {"run_id": run_id, "proposals": [], "dropped": [], "skipped": f"only {len(events)} events with impacts"}
    factors, sectors = deps.store.map_vocabulary()
    ev = await gather(deps.tools, "map_proposer", [
        ("get_sector_sensitivities", {"status": "approved"}),
        ("get_index_history", {"index_code": "SECTOR_INDICES"}),
    ], run_id=run_id, as_of=decision_time)
    current_rows = next((i.data.get("rows", []) for i in ev.items if i.tool == "get_sector_sensitivities"), [])
    current = {(r["factor"], r["sector"]): r["id"] for r in current_rows}
    for (f, sec) in list(current):
        current[(f, sec)] = deps.store.approved_map_row(f, sec) or current[(f, sec)]
    compact = [{"event_id": e["id"], "title": e["title"], "severity": e["severity"], "type": e.get("event_type"),
                "first_seen": e.get("first_seen"),
                "impacts": [{"sector": i["sector"], "direction": i["direction"], "magnitude": i["magnitude"],
                             "basis": i.get("basis"), "channel": i.get("channel")} for i in e["impacts"]]}
               for e in events]
    try:
        out = await deps.llm.structured(
            call_id="LLM-13", agent="map_proposer", run_id=run_id, decision_time=decision_time,
            output_model=MapChangeProposals,
            sections={"factors": as_json(factors), "sectors": as_json(sectors),
                      "current_map": ev.render({"get_sector_sensitivities"}),
                      "sector_moves": ev.render({"get_index_history"}), "recent_events": as_json(compact)},
            cite_ids=ev.ids(), template_vars={"min_obs": str(min_obs)})
    except RECOVERABLE as e:
        deps.store.finish_run(run_id, "degraded", deps.budget.run_spent, {"errors": [str(e)]})
        return {"run_id": run_id, "proposals": [], "dropped": [], "error": str(e)}
    kept, dropped = check_proposals(out, factors=set(factors), sectors=set(sectors),
                                    event_ids={e["id"] for e in events}, min_obs=min_obs, current=current)
    tasks = []
    for p in kept:
        row_id = deps.store.save_map_proposal(p)
        tasks.append(deps.store.create_hitl_task(
            "approve_map_change", "sector_sensitivity", row_id, payload={"proposal": p},
            ai_recommendation="approve" if p["expected_confidence"] >= 0.6 else "review",
            reason=f"{p['action']} {p['factor']} -> {p['sector']}: {p['rationale'][:160]}",
            priority=3, default_on_expiry="keep_current"))
    deps.store.save_agent_output(run_id, "map_proposer", "map", "weekly", {"kept": kept, "dropped": dropped}, "passed", None)
    deps.store.finish_run(run_id, "succeeded", deps.budget.run_spent)
    return {"run_id": run_id, "proposals": kept, "dropped": dropped, "tasks": tasks}
