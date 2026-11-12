"""19:30 digest_publish (§4.2, J1): picks published today (auto or approved in review),
items awaiting review, regime, events, why horizons have no picks, and the auto-follow scorecard."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m

from . import agents
from .deps import Deps
from .graphs.candidate import RECOVERABLE
from .graphs.daily import _render_digest, _template_digest

IST = timezone(timedelta(hours=5, minutes=30))


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), IST)
    return start, start + timedelta(days=1)


def gather_digest_inputs(f: sessionmaker[Session], day: date) -> dict[str, Any]:
    start, end = _day_bounds(day)
    with f() as s:
        published = []
        for r, sym in s.execute(select(m.Recommendation, m.Instrument.symbol).join(m.Instrument).where(
                m.Recommendation.published_at >= start, m.Recommendation.published_at < end)):
            if (r.meta or {}).get("run_type", "daily") in ("daily", "weekly", "monthly"):
                published.append(r.card | {"reco_id": r.id, "confidence": float(r.confidence or 0)})
        pending = s.scalars(select(m.HitlTask).where(m.HitlTask.status == "pending")).all()
        summaries = [a.output for a in s.scalars(select(m.AgentOutput).join(m.Run, m.Run.id == m.AgentOutput.run_id).where(
            m.AgentOutput.agent == "run_summary", m.Run.decision_time >= start, m.Run.decision_time < end))]
        nav = None
        p = s.scalar(select(m.Portfolio).where(m.Portfolio.type == "auto_follow", m.Portfolio.archived.is_(False)))
        if p is not None:
            snap = s.scalar(select(m.DailySnapshot).where(m.DailySnapshot.portfolio_id == p.id)
                            .order_by(m.DailySnapshot.snap_date.desc()).limit(1))
            if snap is not None:
                nav = {"nav": float(snap.nav), "return_pct": round((float(snap.nav) / float(p.starting_cash) - 1) * 100, 2),
                       "benchmark_return_pct": round((float(snap.benchmark_nav) / float(p.starting_cash) - 1) * 100, 2)
                       if snap.benchmark_nav else None, "as_of": snap.snap_date.isoformat()}
    horizons = sorted({h for su in summaries for h in su.get("horizons", [])})
    reasons = {}
    for su in summaries:
        reasons.update(su.get("no_pick_reasons", {}))
    published_h = {c["horizon"] for c in published}
    reasons = {h: r for h, r in reasons.items() if h not in published_h}
    attention = []
    by_type: dict[str, int] = {}
    for t in pending:
        by_type[t.task_type] = by_type.get(t.task_type, 0) + 1
    if by_type:
        attention.append("Awaiting review: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(by_type.items())))
    for su in summaries:
        if not su.get("data_ok", True):
            attention.append("Data quality gate failed: " + "; ".join(su.get("data_issues", [])))
    if not summaries:
        attention.append(f"No daily run recorded for {day.isoformat()}")
    return {"published": published, "reasons": reasons, "attention": attention, "auto_follow": nav,
            "horizons": horizons}


async def publish_digest(deps: Deps, f: sessionmaker[Session], day: date) -> dict[str, Any]:
    inputs = gather_digest_inputs(f, day)
    regime = deps.store.latest_regime()
    events = deps.store.active_events(since=datetime.now(timezone.utc) - timedelta(days=2))
    run_id = deps.store.create_run("digest", datetime.now(IST), "schedule", deps.cfg.version, deps.cfg.strategy_version,
                                   deps.cfg.prompt_bundle_version)
    try:
        dg = (await agents.write_digest(deps, run_id, datetime.now(IST), inputs["published"], regime, events,
                                        inputs["reasons"], inputs["attention"])).model_dump(mode="json")
    except RECOVERABLE:
        dg = _template_digest({"regime": regime}, inputs["published"], inputs["reasons"], inputs["attention"])
    if inputs["auto_follow"]:
        a = inputs["auto_follow"]
        bench = f" vs Nifty 500 {a['benchmark_return_pct']:+.2f}%" if a["benchmark_return_pct"] is not None else ""
        dg["sim"] = f"Auto-follow {a['return_pct']:+.2f}%{bench} (as of {a['as_of']})"
    text = f"Daily digest — {day.strftime('%a %d %b')}\n\n" + _render_digest(dg)
    if dg.get("sim"):
        text = text.replace("\n\nDecision-support only", f"\n\n{dg['sim']}\n\nDecision-support only")
    deps.store.save_agent_output(run_id, "digest", "date", day.isoformat(), {**dg, "text": text,
                                 "reco_ids": [c["reco_id"] for c in inputs["published"]]}, "passed", None)
    await deps.notifier.send("telegram", "digest", text, dg)
    deps.store.finish_run(run_id, "succeeded", deps.budget.run_spent)
    return {"digest": dg, "text": text, "run_id": run_id}
