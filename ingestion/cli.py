"""Data operations CLI.

    stock-primer-data init-db                 # apply migrations (alembic upgrade head)
    stock-primer-data seed                    # cost config, macro factors, config v1
    stock-primer-data calendar --year 2026    # NSE holidays -> trading_calendar
    stock-primer-data universe [--name NIFTY200]
    stock-primer-data eod [--date 2026-10-12]
    stock-primer-data backfill --start 2026-09-01 [--end 2026-10-12]
    stock-primer-data gate [--date 2026-10-12]
    stock-primer-data status
    stock-primer-data context                 # macro, flows, results calendar, world news, RBI/SEBI
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from core import models as m
from core.db import session_factory
from core.settings import get_settings

from .adapters.base import DiskCache
from .adapters.corporate_actions import NseCorporateActions
from .adapters.nse import NseArchives, fetch_holidays
from .adapters.universe import NiftyConstituents
from .adapters.yahoo import YahooFinance
from .eod import run_eod_ingest
from .quality import run_gate
from .reference import is_trading_day, previous_trading_day, seed, sync_calendar, sync_universe, universe_members

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent


def _today_ist() -> date:
    return datetime.now(IST).date()


def _adapters():
    cache = DiskCache(get_settings().cache_dir)
    return NseArchives(cache), YahooFinance(), NseCorporateActions(cache)


def init_db() -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="stock-primer-data")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("seed")
    c = sub.add_parser("calendar")
    c.add_argument("--year", type=int, default=_today_ist().year)
    u = sub.add_parser("universe")
    u.add_argument("--name", default=None)
    u.add_argument("--since", type=date.fromisoformat, default=None,
                   help="backdate membership of names first seen now (bootstrap only; the free list has no "
                        "history, so dates before the first real snapshot carry survivorship bias)")
    e = sub.add_parser("eod")
    e.add_argument("--date", type=date.fromisoformat, default=None)
    b = sub.add_parser("backfill")
    b.add_argument("--start", type=date.fromisoformat, required=True)
    b.add_argument("--end", type=date.fromisoformat, default=None)
    g = sub.add_parser("gate")
    g.add_argument("--date", type=date.fromisoformat, default=None)
    sub.add_parser("status")
    sub.add_parser("context", help="macro series, FII/DII flows, results calendar, world news, RBI/SEBI releases")
    fu = sub.add_parser("fundamentals", help="quarterly results from NSE XBRL")
    fu.add_argument("--symbols", default=None, help="comma-separated; default: the universe")
    fu.add_argument("--quarters", type=int, default=8)
    sh = sub.add_parser("shareholding", help="quarterly shareholding patterns (promoter, FPI, DII, pledge)")
    sh.add_argument("--symbols", default=None, help="comma-separated; default: the universe")
    sh.add_argument("--quarters", type=int, default=8)
    sub.add_parser("balance-sheets", help="fill equity/debt on stored XBRL quarters from the cached filings")
    fp = sub.add_parser("fundamentals-pdf", help="fill missing quarters from results PDFs (LLM-15)")
    fp.add_argument("--symbols", default=None, help="comma-separated; default: every gap in the universe")
    fp.add_argument("--quarters", type=int, default=8)
    fp.add_argument("--limit", type=int, default=None, help="at most this many quarters (each is one LLM call)")
    fp.add_argument("--dry-run", action="store_true", help="find, download and select pages; no LLM call")
    fp.add_argument("--no-ocr", action="store_true", help="skip scanned PDFs instead of OCR'ing them")
    args = p.parse_args(argv)
    from core.net import setup_tls

    setup_tls()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    settings = get_settings()
    f = session_factory()

    if args.cmd == "init-db":
        init_db()
        print("database at", settings.database_url)
    elif args.cmd == "seed":
        with f() as s, s.begin():
            seed(s)
        print("seeded")
    elif args.cmd == "calendar":
        hol = [h for h in fetch_holidays() if h["date"].year == args.year]
        with f() as s, s.begin():
            n = sync_calendar(s, date(args.year, 1, 1), date(args.year, 12, 31), hol)
        print(f"{n} days, {len(hol)} holidays for {args.year}")
    elif args.cmd == "universe":
        name = args.name or settings.universe
        res = NiftyConstituents(DiskCache(settings.cache_dir)).fetch(name, _today_ist())
        with f() as s, s.begin():
            print(json.dumps(sync_universe(s, name, res.records, args.since or _today_ist())))
    elif args.cmd in ("eod", "backfill"):
        nse, yahoo, corp = _adapters()
        if args.cmd == "eod":
            with f() as s:
                day = args.date or (_today_ist() if is_trading_day(s, _today_ist()) else previous_trading_day(s, _today_ist()))
            days = [day]
        else:
            end = args.end or _today_ist()
            days = [args.start + timedelta(days=i) for i in range((end - args.start).days + 1)]
        for d in days:
            rep = run_eod_ingest(f, d, nse=nse, yahoo=yahoo, corp=corp, settings=settings,
                                 ca_lookback_days=30 if args.cmd == "eod" else 1)
            if rep.trading_day:
                print(d, rep.equity_source, rep.equity_rows, "rows,", rep.index_rows, "indices,",
                      len(rep.anomalies), "anomalies", "; ".join(rep.warnings))
    elif args.cmd == "gate":
        with f() as s, s.begin():
            day = args.date or previous_trading_day(s, _today_ist() + timedelta(days=1))
            print(json.dumps(asdict(run_gate(s, day, settings)), default=str, indent=2))
    elif args.cmd == "context":
        from .adapters.macro import YahooMacro
        from .adapters.nse_extra import NseExtra
        from .adapters.world_news import WorldNews
        from .context import run_context_ingest

        rep = run_context_ingest(f, _today_ist(), macro=YahooMacro(), nse_extra=NseExtra(), world=WorldNews(),
                                 macro_days=400)
        print(json.dumps(rep.__dict__, indent=2))
    elif args.cmd == "fundamentals":
        from .adapters.nse_results import NseResults
        from .fundamentals import ingest_symbol, FundamentalsReport

        if args.symbols:
            syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
        else:
            with f() as s:
                syms = sorted(universe_members(s, settings.universe, _today_ist()).values())
        src = NseResults(DiskCache(settings.cache_dir))
        rep = FundamentalsReport()
        for i, sym in enumerate(syms, 1):
            before = rep.stored
            ingest_symbol(f, src, sym, args.quarters, rep)
            print(f"[{i}/{len(syms)}] {sym}: +{rep.stored - before}", flush=True)
        print(json.dumps({"symbols": rep.symbols, "stored": rep.stored, "skipped_existing": rep.skipped_existing,
                          "anomalies": rep.anomalies, "errors": rep.errors}, indent=2))
    elif args.cmd == "shareholding":
        from .adapters.nse_shareholding import NseShareholding
        from .shareholding import ingest_many

        if args.symbols:
            syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
        else:
            with f() as s:
                syms = sorted(universe_members(s, settings.universe, _today_ist()).values())
        from .adapters.bse import Bse

        cache = DiskCache(settings.cache_dir)
        rep = ingest_many(f, NseShareholding(cache), syms, args.quarters, bse=Bse(cache),
                          progress=lambda i, n, sym, added: print(f"[{i}/{n}] {sym}: +{added}", flush=True))
        print(json.dumps({"symbols": rep.symbols, "stored": rep.stored, "updated": rep.updated,
                          "skipped_existing": rep.skipped_existing, "issues": rep.issues, "errors": rep.errors}, indent=2))
    elif args.cmd == "balance-sheets":
        from .adapters.nse_results import NseResults
        from .fundamentals import backfill_balance_sheets

        print(json.dumps(backfill_balance_sheets(f, NseResults(DiskCache(settings.cache_dir)))))
    elif args.cmd == "fundamentals-pdf":
        import asyncio

        from .adapters.nse_announcements import NseAnnouncements
        from .filing_pdf import fill_gaps, find_gaps

        today = _today_ist()
        with f() as s:
            gaps = find_gaps(s, today, args.quarters, settings.universe,
                             [x.strip() for x in args.symbols.split(",")] if args.symbols else None)
        print(f"{len(gaps)} missing quarters past their due date", flush=True)

        async def run():
            from datetime import datetime as _dt

            from orchestrator.wiring import real_deps

            from .filing_pdf import IST

            from .ocr import Ocr, available

            from .adapters.bse import Bse
            from .filing_pdf import ChainedResults, isin_lookup

            cache = DiskCache(settings.cache_dir)
            src = ChainedResults(NseAnnouncements(cache), Bse(cache), isin_lookup(f))      # BSE for BSE-only filers
            ocr = None if args.no_ocr or not available() else Ocr(DiskCache(settings.cache_dir))
            if args.dry_run:
                return await fill_gaps(None, f, src, gaps, _dt.now(IST), dry_run=True, limit=args.limit, ocr=ocr)
            async with real_deps(llm="live", factory=f) as deps:
                return await fill_gaps(deps, f, src, gaps, _dt.now(IST), limit=args.limit, ocr=ocr)
        rep, rows = asyncio.run(run())
        for r in rows:
            print(f"{r['symbol']} {r['period_end']}: {r['status']}"
                  + (f" pages {r['pages']} ({r.get('nature_found', '?')}{', OCR' if r.get('ocr') else ''})" if r.get("pages") else "")
                  + (f" — {'; '.join(r['issues'])}" if r.get("issues") else ""), flush=True)
        print(json.dumps({"tried": rep.tried, "stored": rep.stored, "review": rep.review, "dry_run": rep.dry_run,
                          "skipped": rep.skipped, "errors": rep.errors}, indent=2))
    elif args.cmd == "status":
        with f() as s:
            last = s.scalar(select(func.max(m.PriceDaily.trade_date)))
            print(json.dumps({
                "database": settings.database_url, "instruments": s.scalar(select(func.count()).select_from(m.Instrument)),
                "price_rows": s.scalar(select(func.count()).select_from(m.PriceDaily)), "last_price_date": str(last),
                "trading_days_with_data": s.scalar(select(func.count(func.distinct(m.PriceDaily.trade_date)))),
                "pending_hitl": s.scalar(select(func.count()).select_from(m.HitlTask).where(m.HitlTask.status == "pending")),
                "last_gates": [{"date": str(r.decision_date), "passed": r.passed, "excluded": len(r.excluded)}
                               for r in s.scalars(select(m.DataGateResult).order_by(m.DataGateResult.decision_date.desc()).limit(5))],
            }, indent=2))


if __name__ == "__main__":
    main()
