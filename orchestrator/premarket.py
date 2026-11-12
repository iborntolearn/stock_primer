"""07:30 pre-market brief (§4.2, J1): regime, overnight macro moves, flows, new events,
and open picks that need attention today. Deterministic template - no LLM spend."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m

from .deps import Deps

IST = timezone(timedelta(hours=5, minutes=30))
MACRO_LABELS = {"BRENT": ("Brent", "$"), "USDINR": ("USD/INR", "₹"), "US10Y": ("US 10Y", ""), "GOLD": ("Gold", "$"),
                "DXY": ("Dollar index", "")}
STOP_PROXIMITY = 0.02


def gather_brief(f: sessionmaker[Session], today: date) -> dict[str, Any]:
    with f() as s:
        macro = []
        for sid, (label, unit) in MACRO_LABELS.items():
            rows = s.scalars(select(m.MacroSeries).where(m.MacroSeries.series_id == sid, m.MacroSeries.obs_date < today + timedelta(days=1))
                             .order_by(m.MacroSeries.obs_date.desc()).limit(2)).all()
            if len(rows) == 2:
                last, prev = float(rows[0].value), float(rows[1].value)
                chg = (last - prev) if sid == "US10Y" else (last / prev - 1) * 100
                macro.append({"series": sid, "label": label, "last": last, "change": round(chg, 3),
                              "unit": "pp" if sid == "US10Y" else "%", "date": rows[0].obs_date.isoformat()})
        flow = s.scalar(select(m.FlowsDaily).where(m.FlowsDaily.trade_date < today).order_by(m.FlowsDaily.trade_date.desc()).limit(1))
        since = datetime.combine(today - timedelta(days=1), time(7), IST)
        events = [e for e in s.scalars(select(m.Event).where(m.Event.status.in_(("active", "unverified")), m.Event.severity >= 3))
                  if e.first_seen and e.first_seen >= since]
        attention = []
        week = today + timedelta(days=7)
        for r, sym in s.execute(select(m.Recommendation, m.Instrument.symbol).join(m.Instrument)
                                .where(m.Recommendation.status.in_(("published", "active")))):
            last = s.scalar(select(m.PriceDaily.close).where(m.PriceDaily.instrument_id == r.instrument_id)
                            .order_by(m.PriceDaily.trade_date.desc()).limit(1))
            if last is not None and r.stop is not None and float(last) <= float(r.stop) * (1 + STOP_PROXIMITY):
                attention.append(f"{sym} ({r.horizon}) closed at {float(last):,.2f}, within {STOP_PROXIMITY:.0%} of its stop {float(r.stop):,.2f}")
            res = s.scalar(select(m.CorporateAction.ex_date).where(
                m.CorporateAction.instrument_id == r.instrument_id, m.CorporateAction.action_type == "results",
                m.CorporateAction.ex_date >= today, m.CorporateAction.ex_date <= week))
            if res is not None:
                attention.append(f"{sym} ({r.horizon}) has results on {res.strftime('%d %b')}")
        attention = list(dict.fromkeys(attention))   # one line per stock/horizon even with several open picks
        pending = len(s.scalars(select(m.HitlTask.id).where(m.HitlTask.status == "pending")).all())
        regime = s.scalar(select(m.RegimeSnapshot).order_by(m.RegimeSnapshot.as_of.desc()).limit(1))
    return {
        "date": today.isoformat(),
        "regime": {"label": regime.regime, "as_of": regime.as_of.isoformat(), "narrative": regime.narrative} if regime else None,
        "macro": macro,
        "flows": {"date": flow.trade_date.isoformat(), "fii_net_cr": float(flow.fii_net or 0), "dii_net_cr": float(flow.dii_net or 0)} if flow else None,
        "events": [{"event_id": e.id, "title": e.title, "severity": e.severity, "status": e.status} for e in
                   sorted(events, key=lambda e: -e.severity)[:6]],
        "attention": attention, "pending_reviews": pending,
    }


def render_brief(b: dict[str, Any]) -> str:
    d = datetime.fromisoformat(b["date"]).strftime("%a %d %b")
    lines = [f"Pre-market brief — {d}"]
    if b["regime"]:
        lines.append(f"Regime: {b['regime']['label'].replace('_', '-').upper()} (as of {b['regime']['as_of']})")
    if b["macro"]:
        lines.append("Overnight: " + " · ".join(
            f"{x['label']} {x['last']:,.2f} ({x['change']:+.2f}{x['unit']})" for x in b["macro"]))
    if b["flows"]:
        f = b["flows"]
        lines.append(f"Flows {f['date']}: FII {f['fii_net_cr']:+,.0f} cr · DII {f['dii_net_cr']:+,.0f} cr")
    if b["events"]:
        lines.append("New events: " + "; ".join(f"{e['title']} (sev {e['severity']}{', unverified' if e['status'] == 'unverified' else ''})"
                                                 for e in b["events"]))
    if b["attention"] or b["pending_reviews"]:
        lines.append("Needs attention:")
        lines += [f"• {a}" for a in b["attention"]]
        if b["pending_reviews"]:
            lines.append(f"• {b['pending_reviews']} item(s) in the review inbox")
    lines.append("Decision-support only, not investment advice.")
    return "\n".join(lines)


async def publish_premarket(deps: Deps, f: sessionmaker[Session], today: date) -> dict[str, Any]:
    b = gather_brief(f, today)
    text = render_brief(b)
    run_id = deps.store.create_run("premarket", datetime.combine(today, time(7, 30), IST), "schedule", deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    deps.store.save_agent_output(run_id, "premarket", "date", today.isoformat(), {**b, "text": text}, "passed", None)
    await deps.notifier.send("telegram", "premarket", text, b)
    deps.store.finish_run(run_id, "succeeded", 0.0)
    return {"brief": b, "text": text, "run_id": run_id}
