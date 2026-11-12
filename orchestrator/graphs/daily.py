"""Daily run graph (§4.2 18:30 job, §4.5.2).

data_gate ─┬─ regime ─┐
           └─ events ─┴─ sector ─ screen ─ triage ─(Send per candidate)→ research ─ select ─ digest ─ finalize
     (gate fails) ───────────────────────────────────────────────────────────────────────→ digest ─ finalize

The same graph runs the weekly (medium) and monthly (long) sleeves by passing
different horizons. Nothing here writes to external systems except the store,
the HITL queue and the notifier.
"""
from __future__ import annotations

import asyncio
import logging
import operator
from datetime import datetime, timedelta
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .. import agents
from ..config import Autonomy
from ..deps import Deps
from ..sla import default_due
from .candidate import RECOVERABLE, build_candidate_graph, candidate_input

log = logging.getLogger(__name__)


class DailyState(TypedDict, total=False):
    run_id: int
    decision_time: str
    run_type: str
    horizons: list[str]
    status: str
    data_ok: bool
    data_issues: list
    regime: Optional[dict]
    events: list
    sector_view: Optional[dict]
    candidates: dict           # horizon -> [screen rows]
    dropped: list
    research_inputs: list
    results: Annotated[list, operator.add]
    published: list
    pending: list
    no_pick_reasons: dict
    digest: Optional[dict]
    defer_digest: bool
    errors: Annotated[list, operator.add]


async def screen_horizons(deps: Deps, run_id: int, dt: datetime, horizons: list[str], regime: dict,
                          sector_view: Optional[dict], top_k: int, constraints: Optional[dict] = None) -> dict[str, list]:
    """Deterministic screening through quant-mcp, then levels for each survivor."""
    overlay = {o["sector"]: o["overlay_score"] for o in (sector_view or {}).get("sector_overlay", [])}
    out: dict[str, list] = {}
    for h in horizons:
        r = await deps.tools.call("orchestrator", "screen_universe", {
            "horizon": h, "as_of": dt.isoformat(), "top_k": top_k,
            "constraints": {"regime": regime.get("regime"), "sector_overlay": overlay, **(constraints or {})},
        }, run_id=run_id, as_of=dt)
        rows = r.data.get("candidates", []) if r.ok and r.data else []
        enriched = []
        for row in rows:
            lv = await deps.tools.call("orchestrator", "propose_levels",
                                       {"symbol": row["symbol"], "horizon": h, "as_of": dt.isoformat()},
                                       run_id=run_id, as_of=dt)
            if lv.ok and lv.data:
                enriched.append({**row, "levels": lv.data})
        out[h] = enriched
    return out


def build_daily_graph(deps: Deps, checkpointer=None):
    cfg = deps.cfg
    candidate_graph = build_candidate_graph(deps)

    def dt(s: DailyState) -> datetime:
        return datetime.fromisoformat(s["decision_time"])

    async def data_gate(s: DailyState) -> dict:
        r = await deps.tools.call("orchestrator", "data_quality_status", {"as_of": s["decision_time"]},
                                  run_id=s["run_id"], as_of=dt(s))
        if not r.ok or not r.data:
            return {"data_ok": False, "data_issues": ["data_quality_status unavailable"], "status": "degraded"}
        d = r.data
        horizons = list(s["horizons"])
        if not d.get("is_trading_day", True) and "short" in horizons:
            horizons.remove("short")   # §4.3 calendar check
        if d.get("passed"):
            return {"data_ok": True, "data_issues": d.get("issues", []), "horizons": horizons}
        return {"data_ok": False, "data_issues": d.get("issues", ["data quality gate failed"]), "status": "degraded"}

    def after_gate(s: DailyState):
        return ["regime", "events"] if s.get("data_ok") else "digest"

    async def regime_node(s: DailyState) -> dict:
        try:
            snap, _ = await agents.regime(deps, s["run_id"], dt(s))
        except RECOVERABLE as e:
            # Fall back to the last stored regime rather than failing the run.
            last = deps.store.latest_regime()
            return {"regime": last, "errors": [f"regime: {e}"]}
        deps.store.save_regime(snap["as_of"], snap)
        return {"regime": snap}

    def events_node(s: DailyState) -> dict:
        return {"events": deps.store.active_events(since=dt(s) - timedelta(days=cfg.news_lookback_days))}

    async def sector_node(s: DailyState) -> dict:
        if not s.get("regime"):
            return {"sector_view": None, "errors": ["sector: no regime"]}
        impacts = [i for e in s.get("events", []) for i in e.get("impacts", [])]
        try:
            view, _ = await agents.sector_view(deps, s["run_id"], dt(s), s["regime"], impacts)
        except RECOVERABLE as e:
            return {"sector_view": None, "errors": [f"sector: {e}"]}
        return {"sector_view": view.model_dump(mode="json")}

    async def screen_node(s: DailyState) -> dict:
        if not s.get("regime"):
            return {"candidates": {}, "errors": ["screen: no regime, skipping"]}
        top_k = cfg.top_k_degraded if deps.budget.degraded else cfg.top_k_per_horizon
        cands = await screen_horizons(deps, s["run_id"], dt(s), s["horizons"], s["regime"], s.get("sector_view"), top_k)
        return {"candidates": cands}

    async def triage_node(s: DailyState) -> dict:
        symbols = sorted({c["symbol"] for rows in s.get("candidates", {}).values() for c in rows})
        dropped: list[dict] = []
        if symbols:
            try:
                tri = await agents.triage(deps, s["run_id"], dt(s), symbols)
                dropped = [{"symbol": i.symbol, "red_flags": i.red_flags, "reason": i.reason}
                           for i in tri.items if not i.keep]
            except RECOVERABLE as e:
                # Triage is a filter; if it fails, keep everyone and let deep research judge.
                return {"dropped": [], "errors": [f"triage: {e}"], "research_inputs": _inputs(s, set())}
        drop = {d["symbol"] for d in dropped}
        return {"dropped": dropped, "research_inputs": _inputs(s, drop)}

    def _inputs(s: DailyState, drop: set[str]) -> list[dict]:
        out = []
        for h, rows in s.get("candidates", {}).items():
            for c in rows:
                if c["symbol"] in drop:
                    continue
                out.append(candidate_input(
                    run_id=s["run_id"], decision_time=dt(s), symbol=c["symbol"], horizon=h, sector=c.get("sector"),
                    quant={k: v for k, v in c.items() if k != "levels"}, levels=c["levels"],
                    regime=s.get("regime") or {}, active_events=s.get("events", []), run_type=s.get("run_type", "daily")))
        return out

    def fan_out(s: DailyState):
        inputs = s.get("research_inputs", [])
        if not inputs:
            return "select"
        return [Send("research", i) for i in inputs]

    sem = asyncio.Semaphore(cfg.research_concurrency)

    async def research(payload: dict) -> dict:
        async with sem:
            try:
                out = await candidate_graph.ainvoke(payload)
                return {"results": [out["result"]]}
            except Exception as e:  # one stock must never fail the run
                log.exception("research %s/%s crashed", payload.get("symbol"), payload.get("horizon"))
                return {"results": [{"symbol": payload.get("symbol"), "horizon": payload.get("horizon"),
                                     "status": "failed", "final_score": None, "confidence": 0.0,
                                     "violations": [], "gaps": [str(e)], "card": None, "reco_id": None}],
                        "errors": [f"research {payload.get('symbol')}: {e}"]}

    def select(s: DailyState) -> dict:
        since = dt(s) - timedelta(days=cfg.republish_cooldown_days)
        published, pending, reasons = [], [], {}
        for h in s.get("horizons", []):
            rows = [r for r in s.get("results", []) if r["horizon"] == h]
            ok = sorted([r for r in rows if r["status"] in ("validated", "hitl_pending") and r["verdict"] == "favourable"],
                        key=lambda r: r["final_score"], reverse=True)
            chosen = []
            for r in ok:
                prev = deps.store.recent_reco(r["symbol"], h, since)
                if prev and prev.get("verdict") == r["verdict"] and _levels_close(prev, r["card"]):
                    deps.store.set_reco_status(r["reco_id"], "rejected", close_reason="V10 duplicate")
                    continue
                chosen.append(r)
                if len(chosen) >= cfg.max_published_per_horizon:
                    break
            autonomy = cfg.autonomy_for(h)
            for r in chosen:
                auto = (autonomy >= Autonomy.L2_APPROVE_EXCEPTIONS and r["status"] == "validated"
                        and r["confidence"] >= cfg.publish_confidence_min)
                if auto:
                    deps.store.set_reco_status(r["reco_id"], "published", published_at=dt(s).isoformat())
                    published.append(r)
                else:
                    reason = ("kill switch on" if cfg.kill_switch else
                              "validation exception" if r["status"] == "hitl_pending" else
                              f"autonomy L{int(autonomy)}" if autonomy < Autonomy.L2_APPROVE_EXCEPTIONS else
                              "confidence below threshold")
                    deps.store.set_reco_status(r["reco_id"], "hitl_pending")
                    deps.store.create_hitl_task(
                        "approve_reco", "recommendation", r["reco_id"], payload={"card": r["card"], "result": r},
                        ai_recommendation="approve" if r["status"] == "validated" else "review", reason=reason,
                        priority=2, default_on_expiry="reject", due_at=default_due("approve_reco", dt(s)))
                    pending.append(r)
            if not chosen:
                reasons[h] = _no_pick_reason(s, h, rows)
        return {"published": published, "pending": pending, "no_pick_reasons": reasons}

    async def digest_node(s: DailyState) -> dict:
        if s.get("defer_digest"):
            return {}   # the scheduler's 19:30 digest_publish job sends it after review decisions
        attention = []
        if not s.get("data_ok", True):
            attention.append("Data quality gate failed - no new picks today: " + "; ".join(s.get("data_issues", [])))
        if s.get("pending"):
            attention.append(f"{len(s['pending'])} pick(s) awaiting your review")
        cards = [r["card"] for r in s.get("published", [])]
        reasons = s.get("no_pick_reasons", {}) or ({h: "data quality gate failed" for h in s["horizons"]}
                                                   if not s.get("data_ok", True) else {})
        errors = []
        try:
            dg = await agents.write_digest(deps, s["run_id"], dt(s), cards, s.get("regime"), s.get("events", []),
                                           reasons, attention)
            digest = dg.model_dump(mode="json")
        except RECOVERABLE as e:
            digest = _template_digest(s, cards, reasons, attention)
            errors.append(f"digest: {e}")
        await deps.notifier.send("telegram", "digest", _render_digest(digest), digest)
        return {"digest": digest, "errors": errors}

    def finalize(s: DailyState) -> dict:
        status = s.get("status") or ("degraded" if s.get("errors") else "succeeded")
        deps.store.save_agent_output(s["run_id"], "run_summary", "run", str(s["run_id"]), {
            "horizons": s.get("horizons", []), "data_ok": s.get("data_ok", True), "data_issues": s.get("data_issues", []),
            "no_pick_reasons": s.get("no_pick_reasons", {}) or ({h: "data quality gate failed" for h in s.get("horizons", [])}
                                                                if not s.get("data_ok", True) else {}),
            "published": [r["reco_id"] for r in s.get("published", [])],
            "pending": [r["reco_id"] for r in s.get("pending", [])], "dropped": s.get("dropped", []),
            "errors": s.get("errors", [])}, "passed", None)
        deps.store.finish_run(s["run_id"], status, deps.budget.run_spent,
                              {"errors": s.get("errors", [])} if s.get("errors") else None)
        return {"status": status}

    g = StateGraph(DailyState)
    for name, fn in [("data_gate", data_gate), ("regime", regime_node), ("events", events_node),
                     ("sector", sector_node), ("screen", screen_node), ("triage", triage_node),
                     ("research", research), ("select", select), ("digest", digest_node), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "data_gate")
    g.add_conditional_edges("data_gate", after_gate, ["regime", "events", "digest"])
    g.add_edge(["regime", "events"], "sector")
    g.add_edge("sector", "screen")
    g.add_edge("screen", "triage")
    g.add_conditional_edges("triage", fan_out, ["research", "select"])
    g.add_edge("research", "select")
    g.add_edge("select", "digest")
    g.add_edge("digest", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def _levels_close(prev: dict, card: Optional[dict], tol: float = 0.02) -> bool:
    if not card:
        return True
    for k in ("entry_low", "stop", "target"):
        a, b = prev.get(k), card["levels"].get(k)
        if a is None or b is None or abs(a - b) / max(abs(a), 1e-9) > tol:
            return False
    return True


def _no_pick_reason(s: DailyState, h: str, rows: list[dict]) -> str:
    regime = (s.get("regime") or {}).get("regime")
    screened = len(s.get("candidates", {}).get(h, []))
    if screened == 0:
        return f"no stocks passed the {h}-term gates" + (f" (regime {regime})" if regime else "")
    rejected = sum(1 for r in rows if r["status"] == "rejected")
    weak = sum(1 for r in rows if r.get("verdict") != "favourable")
    return f"{screened} screened, {weak} not favourable after research, {rejected} failed validation"


def _template_digest(s: DailyState, cards: list[dict], reasons: dict, attention: list[str]) -> dict:
    regime = s.get("regime") or {}
    return {
        "attention": attention,
        "market": f"Regime: {regime.get('regime', 'unknown')}.",
        "picks": [{"symbol": c["symbol"], "horizon": c["horizon"],
                   "lines": f"{c['headline']} Entry {c['levels']['entry_low']}-{c['levels']['entry_high']} "
                            f"Stop {c['levels']['stop']} Target {c['levels']['target']}"} for c in cards],
        "no_pick_notes": [f"{h}: {r}" for h, r in reasons.items()],
    }


def _render_digest(d: dict) -> str:
    parts = []
    if d.get("attention"):
        parts.append("NEEDS ATTENTION\n" + "\n".join(f"- {a}" for a in d["attention"]))
    parts.append(d.get("market", ""))
    for p in d.get("picks", []):
        parts.append(f"{p['symbol']} ({p['horizon']}): {p['lines']}")
    if d.get("no_pick_notes"):
        parts.append("No picks: " + "; ".join(d["no_pick_notes"]))
    parts.append("Decision-support only, not investment advice.")
    return "\n\n".join(x for x in parts if x)


async def run_daily(deps: Deps, decision_time: datetime, horizons: Optional[list[str]] = None,
                    run_type: str = "daily", trigger: str = "schedule", checkpointer=None,
                    defer_digest: bool = False) -> dict[str, Any]:
    horizons = horizons or ["short"]
    run_id = deps.store.create_run(run_type, decision_time, trigger, deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    graph = build_daily_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": f"{run_type}:{decision_time.date().isoformat()}:{run_id}"},
              "max_concurrency": deps.cfg.research_concurrency + 4}
    return await graph.ainvoke({"run_id": run_id, "decision_time": decision_time.isoformat(),
                                "run_type": run_type, "horizons": horizons, "results": [], "errors": [],
                                "defer_digest": defer_digest}, config)
