"""Build real dependencies (Deps) from settings: SQL store, tools (in-process or MCP), Claude, notifier.

    async with real_deps(llm="live", transport="inproc") as deps:
        await graphs.analyze(deps, "HDFCBANK", now)
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from typing import AsyncIterator, Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .budget import BudgetManager
from .config import Config
from .deps import Deps
from .ports import AllowlistedTools

IST = timezone(timedelta(hours=5, minutes=30))
MCP_SERVERS = ["market", "quant", "macro", "news", "fundamentals", "evidence", "portfolio"]


class ConsoleNotifier:
    """Prints notifications; replaced by the Telegram notifier when the bot is configured."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.sent: list[dict] = []

    async def send(self, channel, kind, text, payload=None):
        self.sent.append({"channel": channel, "kind": kind, "text": text})
        print(f"[{channel}:{kind}]\n{text}\n", file=self.stream)


def load_config(factory: sessionmaker[Session]) -> Config:
    """Latest approved config version, else defaults."""
    from core import models as m

    with factory() as s:
        row = s.scalar(select(m.ConfigVersion).where(m.ConfigVersion.approved.is_(True))
                       .order_by(m.ConfigVersion.version.desc()).limit(1))
    if row is None:
        return Config()
    cfg = Config.model_validate(row.config)
    cfg.version = row.version
    from quant.factors import STRATEGY_VERSION

    cfg.strategy_version = STRATEGY_VERSION       # the factor code, not a stored setting, defines the strategy
    return cfg


def make_budget(cfg: Config, store) -> BudgetManager:
    """Fresh per-run budget seeded with today's and this month's recorded spend (llm_call)."""
    today = datetime.now(IST).date()
    start_of_day = datetime.combine(today, time(), IST)
    start_of_month = datetime.combine(today.replace(day=1), time(), IST)
    return BudgetManager(cfg.budget_usd_per_run, cfg.budget_usd_per_day, cfg.budget_soft_fraction,
                         day_spent_usd=store.spent_since(start_of_day), month_limit_usd=cfg.budget_usd_per_month,
                         month_spent_usd=store.spent_since(start_of_month))


def fresh_budget(deps: Deps) -> BudgetManager:
    """Long-lived processes (scheduler, API, bot) start every run with a new budget."""
    deps.budget = make_budget(deps.cfg, deps.store)
    if hasattr(deps.llm, "budget"):
        deps.llm.budget = deps.budget
    return deps.budget


@asynccontextmanager
async def real_deps(*, llm: Literal["live", "scripted"] = "live", transport: Literal["inproc", "mcp"] = "inproc",
                    factory: Optional[sessionmaker[Session]] = None, cfg: Optional[Config] = None,
                    live_news: bool = True, notifier=None) -> AsyncIterator[Deps]:
    from core.db import session_factory
    from core.net import setup_tls
    from core.settings import get_settings
    from core.store import SqlStore

    setup_tls()
    factory = factory or session_factory()
    store = SqlStore(factory)
    cfg = cfg or load_config(factory)
    budget = make_budget(cfg, store)

    if llm == "live":
        from .llm import ClaudeLLM

        model = ClaudeLLM(cfg, store, budget)
    else:
        from .fakes import ScriptedLLM

        model = ScriptedLLM()

    if transport == "inproc":
        from ingestion.adapters.gnews import GoogleNews
        from mcp_servers.registry import ToolContext
        from mcp_servers.service import ToolService
        from quant.engine import QuantEngine

        settings = get_settings()
        gw = ToolService(ToolContext(factory=factory, quant=QuantEngine(factory, settings), settings=settings,
                                     news=GoogleNews() if live_news else None), store)
        yield Deps(cfg, AllowlistedTools(gw), store, model, notifier or ConsoleNotifier(), budget)
    else:
        from .mcp_gateway import McpToolGateway

        async with McpToolGateway.stdio(MCP_SERVERS) as gw:
            yield Deps(cfg, AllowlistedTools(gw), store, model, notifier or ConsoleNotifier(), budget)
