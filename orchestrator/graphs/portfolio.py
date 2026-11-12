"""Portfolio review (feature D9, LLM-12): weekly/monthly or event-driven.

review ─ END. Suggestions become HITL tasks; nothing executes automatically (§8.6).
"""
from __future__ import annotations

import operator
from datetime import datetime, timedelta
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import agents
from ..deps import Deps
from .candidate import RECOVERABLE


class PortfolioReviewState(TypedDict, total=False):
    run_id: int
    decision_time: str
    portfolio_ids: list[int]
    reviews: list
    tasks: list
    errors: Annotated[list, operator.add]


def build_portfolio_review_graph(deps: Deps):
    async def review(s: PortfolioReviewState) -> dict:
        dt = datetime.fromisoformat(s["decision_time"])
        events = deps.store.active_events(since=dt - timedelta(days=deps.cfg.news_lookback_days))
        reviews, tasks, errors = [], [], []
        for pid in s["portfolio_ids"]:
            try:
                out, ev = await agents.portfolio_review(deps, s["run_id"], dt, pid, events)
            except RECOVERABLE as e:
                errors.append(f"portfolio {pid}: {e}")
                continue
            known = ev.ids()
            for p in out.positions:
                # §7.2: thesis_broken needs a satisfied invalidation condition backed by evidence.
                backed = any(set(c.evidence_ids) & known for c in p.changes_since_entry)
                if p.status == "thesis_broken" and not backed:
                    p.status = "thesis_at_risk"
                if p.suggested_action != "hold" or p.status in ("thesis_at_risk", "thesis_broken"):
                    tasks.append(deps.store.create_hitl_task(
                        "review_thesis_break", "position", pid,
                        payload={"portfolio_id": pid, "position": p.model_dump(mode="json")},
                        ai_recommendation=p.suggested_action, reason=p.reason, priority=2,
                        default_on_expiry="keep_and_remind"))
            reviews.append(out.model_dump(mode="json"))
            deps.store.save_agent_output(s["run_id"], "portfolio_review", "portfolio", str(pid), reviews[-1],
                                         "passed", None)
        deps.store.finish_run(s["run_id"], "degraded" if errors else "succeeded", deps.budget.run_spent)
        return {"reviews": reviews, "tasks": tasks, "errors": errors}

    g = StateGraph(PortfolioReviewState)
    g.add_node("review", review)
    g.add_edge(START, "review")
    g.add_edge("review", END)
    return g.compile()


async def run_portfolio_review(deps: Deps, decision_time: datetime, portfolio_ids: list[int]) -> dict[str, Any]:
    run_id = deps.store.create_run("portfolio_review", decision_time, "schedule", deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    return await build_portfolio_review_graph(deps).ainvoke(
        {"run_id": run_id, "decision_time": decision_time.isoformat(), "portfolio_ids": portfolio_ids, "errors": []})
