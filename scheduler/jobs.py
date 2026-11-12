"""Scheduled jobs (§4.2). Each is idempotent per (job, decision date) and safe to re-run.

evening_pipeline (18:00 Mon-Fri) chains the doc's 18:00/18:20/18:30 steps so a late
NSE file delays rather than breaks the run:
    eod_ingest (retry every 10 min until 20:00) -> data_quality_gate -> daily_run (short sleeve)
    -> notify review tasks
digest_publish (19:30) -> auto-follow orders for what was published
evaluation_run (23:00) -> simulator EOD (fills, exits, NAV) -> outcome scoring
weekly_run (Sat 10:00, medium) and monthly_run (first Saturday, long) decide as of the last trading close.
hitl_expiry (every 5 min) applies the fail-safe defaults; run_overdue (19:15) alerts if no daily run.
"""
from __future__ import annotations

import asyncio
import sqlite3
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m
from core.settings import Settings, get_settings
from evaluation.outcomes import score_outcomes
from hitl.service import HitlService
from ingestion.eod import run_eod_ingest
from ingestion.quality import run_gate
from ingestion.reference import is_trading_day, previous_trading_day
from orchestrator.deps import Deps
from orchestrator.digest import publish_digest
from orchestrator.graphs import run_daily
from orchestrator.wiring import fresh_budget
from simulator.engine import Simulator

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Runtime:
    deps: Deps
    f: sessionmaker[Session]
    settings: Settings = field(default_factory=get_settings)
    adapters: Optional[Callable[[], tuple]] = None     # () -> (nse, yahoo, corp)
    context_adapters: Optional[Callable[[], tuple]] = None   # () -> (macro, nse_extra, world)
    reload_config: bool = False      # real scheduler: pick up newly approved config (autonomy, budgets) before each run
    now: Callable[[], datetime] = lambda: datetime.now(IST)
    sleep: Callable[[float], Any] = asyncio.sleep

    @property
    def sim(self) -> Simulator:
        return Simulator(self.f)

    @property
    def hitl(self) -> HitlService:
        return HitlService(self.f, self.deps.cfg.rr_floor)

    def make_adapters(self):
        if self.adapters:
            return self.adapters()
        from ingestion.adapters.base import DiskCache
        from ingestion.adapters.corporate_actions import NseCorporateActions
        from ingestion.adapters.nse import NseArchives
        from ingestion.adapters.yahoo import YahooFinance

        cache = DiskCache(self.settings.cache_dir)
        return NseArchives(cache), YahooFinance(), NseCorporateActions(cache)


def _prep(rt: Runtime) -> None:
    """Before each run: latest approved config (if enabled) and a fresh budget."""
    if rt.reload_config:
        from orchestrator.wiring import load_config

        rt.deps.cfg = load_config(rt.f)
        if hasattr(rt.deps.llm, "cfg"):
            rt.deps.llm.cfg = rt.deps.cfg
    fresh_budget(rt.deps)


def _done_today(rt: Runtime, run_type: str, day: date) -> bool:
    with rt.f() as s:
        runs = s.scalars(select(m.Run).where(m.Run.run_type == run_type, m.Run.status.in_(("succeeded", "degraded")))).all()
        return any(r.decision_time.astimezone(IST).date() == day for r in runs)


async def notify(rt: Runtime, kind: str, text: str, payload: Optional[dict] = None) -> None:
    try:
        await rt.deps.notifier.send("telegram", kind, text, payload)
    except Exception:
        log.exception("notification failed")


async def notify_new_tasks(rt: Runtime, since: datetime) -> int:
    """One message per new review task, with decision buttons (Telegram) or as plain text."""
    with rt.f() as s:
        tasks = s.scalars(select(m.HitlTask).where(m.HitlTask.status == "pending", m.HitlTask.created_at >= since)
                          .order_by(m.HitlTask.priority, m.HitlTask.id)).all()
        items = [rt.hitl._dict(t) for t in tasks]
    for t in items:
        headline = (t["payload"].get("card") or {}).get("headline") or t["payload"].get("symbol") or ""
        due = datetime.fromisoformat(t["due_at"]).astimezone(IST).strftime("%H:%M") if t["due_at"] else "-"
        await notify(rt, "hitl_task", f"Review #{t['id']} {t['task_type'].replace('_', ' ')}: {headline}\n"
                                      f"Why: {t['reason']}\nAI suggests: {t['ai_recommendation']} · due {due} "
                                      f"(default: {t['default_on_expiry']})",
                     {"task_id": t["id"], "actions": t["allowed_actions"]})
    return len(items)


async def evening_pipeline(rt: Runtime, day: Optional[date] = None, max_attempts: int = 12,
                           retry_seconds: float = 600) -> dict[str, Any]:
    day = day or rt.now().date()
    with rt.f() as s:
        if not is_trading_day(s, day):
            return {"skipped": "not a trading day"}
    if _done_today(rt, "daily", day):
        return {"skipped": "daily run already done"}
    started = datetime.now(timezone.utc)
    nse, yahoo, corp = rt.make_adapters()
    rep = None
    for attempt in range(max_attempts):
        rep = await asyncio.to_thread(run_eod_ingest, rt.f, day, nse=nse, yahoo=yahoo, corp=corp, settings=rt.settings)
        if not rep.trading_day or rep.equity_rows:
            break
        log.info("bhavcopy for %s not available yet (attempt %d)", day, attempt + 1)
        await rt.sleep(retry_seconds)
    if rep is not None and not rep.trading_day:
        return {"skipped": "no bhavcopy published (holiday)"}
    with rt.f() as s, s.begin():
        gate = run_gate(s, day, rt.settings)
    if not gate.passed:
        await notify(rt, "alert", f"⚠ Data quality gate failed for {day}: " + "; ".join(gate.issues)
                     + "\nNo new picks will be published today.")
    _prep(rt)
    decision = datetime.combine(day, time(18, 30), IST)
    out = await run_daily(rt.deps, decision, horizons=["short"], defer_digest=True)
    n = await notify_new_tasks(rt, started)
    shadow = []
    if gate.passed:
        from orchestrator.shadow import quant_shadow_picks

        rt.sim.ensure_standard_portfolios()
        picks = await quant_shadow_picks(rt.deps, decision, "short", run_id=out.get("run_id"))
        shadow = rt.sim.sync_quant_shadow(decision, picks)
    return {"ingest_rows": rep.equity_rows if rep else 0, "gate_passed": gate.passed, "run_status": out["status"],
            "pending": len(out.get("pending", [])), "published": len(out.get("published", [])), "tasks_notified": n,
            "shadow_orders": len(shadow)}


async def sleeve_run(rt: Runtime, horizon: str, run_type: str) -> dict[str, Any]:
    """Weekly (medium) / monthly (long) refresh, decided as of the last trading close."""
    with rt.f() as s:
        last = previous_trading_day(s, rt.now().date() + timedelta(days=1))
    if _done_today(rt, run_type, last):
        return {"skipped": f"{run_type} already done for {last}"}
    started = datetime.now(timezone.utc)
    _prep(rt)
    out = await run_daily(rt.deps, datetime.combine(last, time(18, 30), IST), horizons=[horizon],
                          run_type=run_type, defer_digest=True)
    await notify_new_tasks(rt, started)
    return {"run_status": out["status"], "pending": len(out.get("pending", [])), "published": len(out.get("published", []))}


async def digest_job(rt: Runtime, day: Optional[date] = None) -> dict[str, Any]:
    day = day or rt.now().date()
    expired = rt.hitl.expire_due(rt.now())                           # undecided picks are not published (§8.4)
    _prep(rt)
    out = await publish_digest(rt.deps, rt.f, day)
    orders = rt.sim.sync_auto_follow(rt.now())
    return {"expired": len(expired), "picks": len(out["digest"].get("picks", [])), "auto_follow_orders": len(orders)}


async def evaluation_job(rt: Runtime, day: Optional[date] = None) -> dict[str, Any]:
    day = day or rt.now().date()
    with rt.f() as s:
        if not is_trading_day(s, day):
            return {"skipped": "not a trading day"}
    reports = await asyncio.to_thread(rt.sim.run_eod, day)
    outcomes = await asyncio.to_thread(score_outcomes, rt.f, day)
    exits = [e for r in reports for e in r.exits]
    if exits:
        await notify(rt, "alert", "Simulated exits today:\n" + "\n".join(f"• {e}" for e in exits))
    return {"portfolios": len(reports), "fills": sum(len(r.filled) for r in reports), "exits": len(exits),
            "scored": len(outcomes.scored), "activated": len(outcomes.activated)}


async def hitl_expiry_job(rt: Runtime) -> int:
    res = rt.hitl.expire_due(rt.now())
    for r in res:
        if r.task_type == "high_severity_event" and r.action == "shadow_run":
            await notify(rt, "alert", f"Event review #{r.task_id} expired; shadow re-evaluation requested.")
    return len(res)


async def run_overdue_job(rt: Runtime) -> bool:
    day = rt.now().date()
    with rt.f() as s:
        if not is_trading_day(s, day):
            return False
    if not _done_today(rt, "daily", day):
        await notify(rt, "alert", f"⚠ Daily run for {day} has not finished by 19:15 IST.")
        return True
    return False


# ---------------------------------------------------------------- Phase 3: mornings and the map
def _context_adapters(rt: Runtime):
    if getattr(rt, "context_adapters", None):
        return rt.context_adapters()
    from ingestion.adapters.macro import YahooMacro
    from ingestion.adapters.nse_extra import NseExtra
    from ingestion.adapters.world_news import WorldNews

    return YahooMacro(), NseExtra(), WorldNews()


async def overnight_job(rt: Runtime, day: Optional[date] = None) -> dict[str, Any]:
    """06:30: macro series, flows, results calendar, world news and official releases."""
    from ingestion.context import run_context_ingest

    day = day or rt.now().date()
    macro, extra, world = _context_adapters(rt)
    rep = await asyncio.to_thread(run_context_ingest, rt.f, day, macro=macro, nse_extra=extra, world=world)
    return rep.__dict__


async def event_digest_job(rt: Runtime) -> dict[str, Any]:
    """07:15: cluster world news into structured events and sector impacts; alert on high severity."""
    from orchestrator.graphs import run_event_digest

    started = datetime.now(timezone.utc)
    _prep(rt)
    out = await run_event_digest(rt.deps, rt.now())
    triggered = None
    if out.get("triggers"):
        triggered = await event_rerun(rt, out["triggers"], reason="event digest")
    n = await notify_new_tasks(rt, started)
    return {"new_events": len(out.get("new_events", [])), "triggers": out.get("triggers", []),
            "rerun": triggered, "tasks_notified": n, "errors": out.get("errors", [])}


async def event_rerun(rt: Runtime, event_ids: list[int], reason: str) -> dict[str, Any]:
    """Shadow re-evaluation of open picks exposed to these events (review tasks only, never publishes),
    followed by a portfolio review of simulated holdings."""
    from orchestrator.graphs import run_event_triggered, run_portfolio_review

    _prep(rt)
    out = await run_event_triggered(rt.deps, rt.now(), event_ids)
    with rt.f() as s:
        pids = sorted(set(s.scalars(select(m.Position.portfolio_id).where(m.Position.status == "open"))))
    review = None
    if pids:
        _prep(rt)
        review = await run_portfolio_review(rt.deps, rt.now(), pids)
    return {"reason": reason, "events": event_ids, "reevaluated": len(out.get("results", [])),
            "review_tasks": len(out.get("tasks", [])) + len((review or {}).get("tasks", []))}


async def pending_event_runs_job(rt: Runtime) -> list[dict[str, Any]]:
    """Run re-evaluations a reviewer asked for (trigger_run) or that an expired event review defaulted to (shadow_run)."""
    with rt.f() as s:
        tasks = [t for t in s.scalars(select(m.HitlTask).where(m.HitlTask.task_type == "high_severity_event",
                                                               m.HitlTask.status.in_(("approved", "expired"))))
                 if (t.decision or {}).get("action") in ("trigger_run", "shadow_run") and not (t.decision or {}).get("run_done")]
        todo = [(t.id, t.subject_id) for t in tasks]
    out = []
    for tid, event_id in todo:
        res = await event_rerun(rt, [event_id], reason=f"review task #{tid}")
        with rt.f() as s, s.begin():
            t = s.get(m.HitlTask, tid)
            t.decision = {**(t.decision or {}), "run_done": True}
        out.append(res)
    if out:
        await notify_new_tasks(rt, datetime.now(timezone.utc) - timedelta(minutes=30))
    return out


async def portfolio_review_job(rt: Runtime) -> dict[str, Any]:
    from orchestrator.graphs import run_portfolio_review

    started = datetime.now(timezone.utc)
    with rt.f() as s:
        pids = sorted(set(s.scalars(select(m.Position.portfolio_id).where(m.Position.status == "open"))))
    if not pids:
        return {"skipped": "no open positions"}
    _prep(rt)
    out = await run_portfolio_review(rt.deps, rt.now(), pids)
    await notify_new_tasks(rt, started)
    return {"portfolios": len(pids), "tasks": len(out.get("tasks", [])), "errors": out.get("errors", [])}


async def premarket_job(rt: Runtime, day: Optional[date] = None) -> dict[str, Any]:
    from orchestrator.premarket import publish_premarket

    day = day or rt.now().date()
    with rt.f() as s:
        if not is_trading_day(s, day):
            return {"skipped": "not a trading day"}
    out = await publish_premarket(rt.deps, rt.f, day)
    return {"attention": len(out["brief"]["attention"]), "events": len(out["brief"]["events"])}


async def map_review_job(rt: Runtime) -> dict[str, Any]:
    from orchestrator.map_review import run_map_review

    started = datetime.now(timezone.utc)
    _prep(rt)
    out = await run_map_review(rt.deps, rt.now())
    await notify_new_tasks(rt, started)
    return {"proposals": len(out.get("proposals", [])), "dropped": len(out.get("dropped", [])),
            "skipped": out.get("skipped")}


async def late_context_job(rt: Runtime, day: Optional[date] = None) -> dict[str, Any]:
    """23:00 companion to the evaluation run: NSE publishes the day's FII/DII figures in the evening."""
    from ingestion.context import run_context_ingest

    day = day or rt.now().date()
    _, extra, _ = _context_adapters(rt)
    rep = await asyncio.to_thread(run_context_ingest, rt.f, day, nse_extra=extra)
    return rep.__dict__


async def results_followup_job(rt: Runtime, day: Optional[date] = None) -> dict[str, Any]:
    """21:30: fetch the XBRL results of companies whose board met in the last few days."""
    from ingestion.adapters.base import DiskCache
    from ingestion.adapters.nse_results import NseResults
    from ingestion.fundamentals import FundamentalsReport, ingest_symbol, symbols_with_recent_results

    day = day or rt.now().date()
    with rt.f() as s:
        syms = symbols_with_recent_results(s, day)
    src = getattr(rt, "results_source", None) or NseResults(DiskCache(rt.settings.cache_dir))
    rep = FundamentalsReport()
    for sym in syms:
        await asyncio.to_thread(ingest_symbol, rt.f, src, sym, 4, rep)
    return {"symbols": syms, "stored": rep.stored, "anomalies": rep.anomalies, "errors": rep.errors}


MARKET_OPEN, MARKET_CLOSE = time(9, 15), time(15, 30)


async def intraday_alerts_job(rt: Runtime) -> dict[str, Any]:
    """Optional (config.intraday_alerts): delayed quotes for watched stops and targets; alerts any hour (§10)."""
    from orchestrator.wiring import load_config
    from simulator.alerts import check, watchlist

    cfg = load_config(rt.f) if rt.reload_config else rt.deps.cfg
    if not cfg.intraday_alerts:
        return {"skipped": "intraday alerts are off"}
    now = rt.now()
    with rt.f() as s:
        if not is_trading_day(s, now.date()):
            return {"skipped": "not a trading day"}
        symbols = sorted({w.symbol for w in watchlist(s)})
    if not MARKET_OPEN <= now.time() <= MARKET_CLOSE:
        return {"skipped": "market closed"}
    if not symbols:
        return {"checked": 0, "alerts": []}
    src = getattr(rt, "quote_source", None)
    if src is None:
        from ingestion.adapters.intraday import YahooIntraday

        src = rt.quote_source = YahooIntraday()
    quotes, warnings = await asyncio.to_thread(src.quotes, symbols)
    alerts = check(rt.f, quotes, now.date(), cfg.stop_proximity_pct)
    for a in alerts:
        await notify(rt, "alert", a["text"], {"alert": a["kind"], "symbol": a["symbol"]})
    return {"checked": len(quotes), "alerts": [a["kind"] + ":" + a["symbol"] for a in alerts], "warnings": warnings}


async def backup_job(rt: Runtime) -> dict[str, Any]:
    """02:30 nightly: encrypted, integrity-checked database backup. Alerts only when something is wrong."""
    from ops.backup import BackupError, run_backup

    try:
        res = await asyncio.to_thread(run_backup, rt.settings, rt.now())
    except (BackupError, OSError, sqlite3.Error) as e:
        await notify(rt, "alert", f"⛔ Nightly backup failed: {e}")
        return {"ok": False, "error": str(e)}
    if res.warnings:
        await notify(rt, "alert", "⚠️ Backup made with warnings: " + "; ".join(res.warnings))
    return {"ok": True, "file": res.file.name, "size": res.entry["size"], "pruned": len(res.pruned), "warnings": res.warnings}


async def backup_verify_job(rt: Runtime) -> dict[str, Any]:
    """Sunday 03:30: restore the latest backup into a scratch file and integrity-check it (an untested backup isn't one)."""
    from ops.backup import BackupError, verify

    try:
        out = await asyncio.to_thread(verify, rt.settings)
    except (BackupError, OSError, sqlite3.Error) as e:
        await notify(rt, "alert", f"⛔ Backup restore test failed: {e}")
        return {"ok": False, "error": str(e)}
    return out


async def results_pdf_job(rt: Runtime, limit: int = 20) -> dict[str, Any]:
    """Saturday 13:00: quarters still missing after their SEBI due date -> results PDF -> LLM-15 -> checks.
    Capped per run (each quarter is one small-model call); anything doubtful goes to the review inbox."""
    import os

    from ingestion.adapters.base import DiskCache
    from ingestion.adapters.nse_announcements import NseAnnouncements
    from ingestion.filing_pdf import fill_gaps, find_gaps

    if not (rt.settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")) and not getattr(rt, "pdf_source", None):
        return {"skipped": "no Anthropic API key"}
    now = rt.now()
    with rt.f() as s:
        gaps = find_gaps(s, now.date(), 8, rt.settings.universe)
    if not gaps:
        return {"gaps": 0}
    if getattr(rt, "pdf_source", None) is not None:
        src = rt.pdf_source
    else:
        from ingestion.adapters.bse import Bse
        from ingestion.filing_pdf import ChainedResults, isin_lookup

        cache = DiskCache(rt.settings.cache_dir)
        src = ChainedResults(NseAnnouncements(cache), Bse(cache), isin_lookup(rt.f))
    _prep(rt)
    started = datetime.now(timezone.utc)
    from ingestion.ocr import Ocr, available

    ocr = getattr(rt, "ocr", None) or (Ocr(DiskCache(rt.settings.cache_dir)) if available() else None)
    rep, _ = await fill_gaps(rt.deps, rt.f, src, gaps, now, limit=limit, ocr=ocr)
    if rep.review:
        await notify_new_tasks(rt, started)
    return {"gaps": len(gaps), "tried": rep.tried, "stored": rep.stored, "review": rep.review,
            "skipped": rep.skipped, "errors": rep.errors}


async def shareholding_job(rt: Runtime) -> dict[str, Any]:
    """Sunday 11:00: new or revised shareholding patterns (due 21 days after each quarter) for the universe."""
    from ingestion.adapters.base import DiskCache
    from ingestion.adapters.nse_shareholding import NseShareholding
    from ingestion.reference import universe_members
    from ingestion.shareholding import ingest_many

    with rt.f() as s:
        syms = sorted(universe_members(s, rt.settings.universe, rt.now().date()).values())
    from ingestion.adapters.bse import Bse

    cache = DiskCache(rt.settings.cache_dir)
    src = getattr(rt, "shareholding_source", None) or NseShareholding(cache)
    bse = getattr(rt, "bse_source", None) or Bse(cache)
    rep = await asyncio.to_thread(lambda: ingest_many(rt.f, src, syms, 2, bse=bse))
    if rep.issues:
        await notify(rt, "alert", f"Shareholding: {len(rep.issues)} filing(s) failed checks and were skipped, e.g. {rep.issues[0]}")
    return {"symbols": rep.symbols, "stored": rep.stored, "updated": rep.updated, "issues": rep.issues, "errors": rep.errors[:10]}
