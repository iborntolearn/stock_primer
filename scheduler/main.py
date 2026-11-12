"""Run the scheduler (APScheduler, Asia/Kolkata).

    python -m scheduler.main            # live Claude; Telegram if configured, else console
    python -m scheduler.main --once evening|digest|evaluation|weekly|monthly
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import jobs

IST = timezone(timedelta(hours=5, minutes=30))
TZ = "Asia/Kolkata"


def add_jobs(sched: AsyncIOScheduler, rt: jobs.Runtime) -> None:
    opts = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}
    sched.add_job(jobs.evening_pipeline, CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone=TZ),
                  args=[rt], id="evening_pipeline", **opts)
    sched.add_job(jobs.run_overdue_job, CronTrigger(day_of_week="mon-fri", hour=19, minute=15, timezone=TZ),
                  args=[rt], id="run_overdue", **opts)
    sched.add_job(jobs.digest_job, CronTrigger(day_of_week="mon-fri", hour=19, minute=30, timezone=TZ),
                  args=[rt], id="digest_publish", **opts)
    sched.add_job(jobs.evaluation_job, CronTrigger(day_of_week="mon-fri", hour=23, minute=0, timezone=TZ),
                  args=[rt], id="evaluation_run", **opts)
    sched.add_job(jobs.sleeve_run, CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=TZ),
                  args=[rt, "medium", "weekly"], id="weekly_run", **opts)
    sched.add_job(jobs.sleeve_run, CronTrigger(day="1-7", day_of_week="sat", hour=11, minute=0, timezone=TZ),
                  args=[rt, "long", "monthly"], id="monthly_run", **opts)
    sched.add_job(jobs.hitl_expiry_job, "interval", minutes=5, args=[rt], id="hitl_expiry", **opts)
    # Phase 3: mornings, flows and the weekly map review
    sched.add_job(jobs.overnight_job, CronTrigger(day_of_week="mon-fri", hour=6, minute=30, timezone=TZ),
                  args=[rt], id="global_overnight_ingest", **opts)
    sched.add_job(jobs.event_digest_job, CronTrigger(day_of_week="mon-fri", hour=7, minute=15, timezone=TZ),
                  args=[rt], id="event_digest", **opts)
    sched.add_job(jobs.premarket_job, CronTrigger(day_of_week="mon-fri", hour=7, minute=30, timezone=TZ),
                  args=[rt], id="premarket_brief", **opts)
    sched.add_job(jobs.late_context_job, CronTrigger(day_of_week="mon-fri", hour=22, minute=45, timezone=TZ),
                  args=[rt], id="late_context", **opts)
    sched.add_job(jobs.map_review_job, CronTrigger(day_of_week="sun", hour=10, minute=0, timezone=TZ),
                  args=[rt], id="map_review", **opts)
    # Phase 4: event-driven re-evaluation, portfolio review, results follow-up
    sched.add_job(jobs.pending_event_runs_job, "interval", minutes=15, args=[rt], id="pending_event_runs", **opts)
    sched.add_job(jobs.portfolio_review_job, CronTrigger(day_of_week="sat", hour=12, minute=0, timezone=TZ),
                  args=[rt], id="portfolio_review", **opts)
    sched.add_job(jobs.results_followup_job, CronTrigger(day_of_week="mon-sat", hour=21, minute=30, timezone=TZ),
                  args=[rt], id="results_followup", **opts)
    # Phase 5: optional intraday stop-proximity alerts (the job itself checks the config switch and market hours)
    sched.add_job(jobs.intraday_alerts_job, CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/15", timezone=TZ),
                  args=[rt], id="intraday_alerts", **opts)
    sched.add_job(jobs.shareholding_job, CronTrigger(day_of_week="sun", hour=11, minute=0, timezone=TZ),
                  args=[rt], id="shareholding", **opts)
    sched.add_job(jobs.results_pdf_job, CronTrigger(day_of_week="sat", hour=13, minute=0, timezone=TZ),
                  args=[rt], id="results_pdf", **opts)
    # Backups (§11)
    sched.add_job(jobs.backup_job, CronTrigger(hour=2, minute=30, timezone=TZ), args=[rt], id="backup", **opts)
    sched.add_job(jobs.backup_verify_job, CronTrigger(day_of_week="sun", hour=3, minute=30, timezone=TZ),
                  args=[rt], id="backup_verify", **opts)


def make_notifier():
    from core.settings import get_settings

    s = get_settings()
    if s.telegram_bot_token and s.telegram_owner_chat_id:
        from bot.notifier import TelegramNotifier

        return TelegramNotifier(s.telegram_bot_token, s.telegram_owner_chat_id)
    return None


async def amain(once: str | None) -> None:
    from core.db import session_factory
    from orchestrator.wiring import real_deps

    f = session_factory()
    async with real_deps(llm="live", factory=f, notifier=make_notifier()) as deps:
        rt = jobs.Runtime(deps=deps, f=f, reload_config=True)
        if once:
            fn = {"evening": jobs.evening_pipeline, "digest": jobs.digest_job, "evaluation": jobs.evaluation_job,
                  "weekly": lambda r: jobs.sleeve_run(r, "medium", "weekly"),
                  "monthly": lambda r: jobs.sleeve_run(r, "long", "monthly"), "overnight": jobs.overnight_job,
                  "events": jobs.event_digest_job, "premarket": jobs.premarket_job, "map": jobs.map_review_job,
                  "portfolio_review": jobs.portfolio_review_job, "results": jobs.results_followup_job,
                  "alerts": jobs.intraday_alerts_job, "backup": jobs.backup_job, "backup_verify": jobs.backup_verify_job,
                  "results_pdf": jobs.results_pdf_job, "shareholding": jobs.shareholding_job}[once]
            print(await fn(rt))
            return
        sched = AsyncIOScheduler(timezone=TZ)
        add_jobs(sched, rt)
        sched.start()
        for j in sched.get_jobs():
            logging.info("scheduled %s next at %s", j.id, j.next_run_time)
        await asyncio.Event().wait()


def main() -> None:
    from core.net import setup_tls

    setup_tls()
    p = argparse.ArgumentParser()
    p.add_argument("--once", choices=["evening", "digest", "evaluation", "weekly", "monthly", "overnight", "events",
                                      "premarket", "map", "portfolio_review", "results", "alerts", "backup",
                                      "backup_verify", "results_pdf", "shareholding"])
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(amain(args.once))


if __name__ == "__main__":
    main()
