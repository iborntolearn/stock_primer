"""Command line entry point.

    stock-primer [--env fake|local] [--llm scripted|live] [--transport inproc|mcp] <command>

    daily [--horizons short,medium]
    analyze HDFCBANK [--horizons short]
    chat "what should I look at now?"
    events
    replay RUN_ID                 # re-run a recorded analysis from stored evidence; no network, no cost
    graph daily|candidate|on_demand|chat|events|event_triggered|portfolio

--env fake  : in-memory store, synthetic tools (no data needed)
--env local : the SQLite/Postgres database filled by stock-primer-data, real tools
--llm live calls Claude (costs money; needs ANTHROPIC_API_KEY). Default: live for local, scripted for fake.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from .config import Config
from .fakes import fake_deps

IST = timezone(timedelta(hours=5, minutes=30))


def _graph(name: str):
    from . import graphs

    deps = fake_deps()
    builders = {
        "daily": graphs.build_daily_graph, "candidate": graphs.build_candidate_graph,
        "on_demand": graphs.build_on_demand_graph, "chat": graphs.build_chat_graph,
        "events": graphs.build_event_digest_graph, "event_triggered": graphs.build_event_triggered_graph,
        "portfolio": graphs.build_portfolio_review_graph,
    }
    return builders[name](deps)


@asynccontextmanager
async def _deps(env: str, llm: str, transport: str):
    if env == "fake":
        deps = fake_deps(Config())
        if llm == "live":
            from .llm import ClaudeLLM

            deps.llm = ClaudeLLM(deps.cfg, deps.store, deps.budget)
        yield deps
    else:
        from .wiring import real_deps

        async with real_deps(llm=llm, transport=transport) as deps:
            yield deps


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="stock-primer")
    p.add_argument("--env", choices=["fake", "local"], default="fake")
    p.add_argument("--llm", choices=["scripted", "live"], default=None)
    p.add_argument("--transport", choices=["inproc", "mcp"], default="inproc")
    p.add_argument("--live-llm", action="store_true", help="deprecated alias for --llm live")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily")
    d.add_argument("--horizons", default="short")
    a = sub.add_parser("analyze")
    a.add_argument("query")
    a.add_argument("--horizons", default="short,medium,long")
    c = sub.add_parser("chat")
    c.add_argument("message")
    sub.add_parser("events")
    r = sub.add_parser("replay")
    r.add_argument("run_id", type=int)
    g = sub.add_parser("graph")
    g.add_argument("name")
    args = p.parse_args(argv)
    from core.net import setup_tls

    setup_tls()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    llm = "live" if args.live_llm else (args.llm or ("live" if args.env == "local" else "scripted"))

    if args.cmd == "graph":
        print(_graph(args.name).get_graph().draw_mermaid())
        return
    if args.cmd == "replay":
        from core.db import session_factory

        from .replay import replay_analysis
        from .wiring import load_config

        f = session_factory()
        print(json.dumps(asyncio.run(replay_analysis(f, load_config(f), args.run_id)), indent=2))
        return

    asyncio.run(_run(args, llm))


async def _run(args, llm: str) -> None:
    from . import graphs
    from .graphs.chat import _summarise

    now = datetime.now(IST)
    async with _deps(args.env, llm, args.transport) as deps:
        if args.cmd == "daily":
            out = await graphs.run_daily(deps, now, horizons=args.horizons.split(","))
            summary = {"status": out["status"], "published": len(out.get("published", [])),
                       "pending_review": len(out.get("pending", [])), "errors": out.get("errors", [])}
        elif args.cmd == "analyze":
            out = await graphs.analyze(deps, args.query, now, args.horizons.split(","))
            print(out.get("clarification") or _summarise(out.get("results", []), out.get("errors")))
            summary = {"run_id": out["run_id"], "errors": out.get("errors", []),
                       "violations": {f"{r['symbol']}/{r['horizon']}": [v["code"] + ":" + v["action"] for v in r["violations"]]
                                      for r in out.get("results", []) if r["violations"]}}
        elif args.cmd == "chat":
            out = await graphs.chat(deps, args.message, now)
            print(out["reply"])
            summary = {"intent": out["route"]["intent"]}
        else:
            out = await graphs.run_event_digest(deps, now)
            summary = {"new_events": len(out.get("new_events", [])), "triggers": out.get("triggers", []),
                       "errors": out.get("errors", [])}
        summary["llm_spend_usd"] = round(deps.budget.run_spent, 4)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
