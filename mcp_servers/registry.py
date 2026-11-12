"""Tool registry, shared context and result types."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session, sessionmaker

IST = timezone(timedelta(hours=5, minutes=30))
MAX_PRICE_ROWS = 400
MAX_NEWS_ITEMS = 20


class ToolFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, retry_after_s: Optional[int] = None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.retry_after_s = code, message, retryable, retry_after_s


@dataclass
class Out:
    data: Any
    kind: str
    source: str
    as_of: Optional[datetime] = None        # when the underlying data was current; defaults to the call's as_of
    untrusted: bool = False
    data_quality: str = "ok"
    warnings: list[str] = field(default_factory=list)


@dataclass
class ToolDef:
    name: str
    server: str
    fn: Callable[..., Out]


@dataclass
class ToolContext:
    factory: sessionmaker[Session]
    quant: Any                       # quant.engine.QuantEngine
    settings: Any
    news: Any = None                 # ingestion.adapters.gnews.GoogleNews (None = store only)


TOOLS: dict[str, ToolDef] = {}
_ctx: Optional[ToolContext] = None


def tool(server: str):
    def deco(fn: Callable[..., Out]) -> Callable[..., Out]:
        TOOLS[fn.__name__] = ToolDef(fn.__name__, server, fn)
        return fn
    return deco


def set_context(c: ToolContext) -> None:
    global _ctx
    _ctx = c


def ctx() -> ToolContext:
    if _ctx is None:
        raise RuntimeError("tool context not initialised")
    return _ctx


def decision_dt(as_of: str) -> datetime:
    if not as_of:
        return datetime.now(IST)
    dt = datetime.fromisoformat(as_of)
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def decision_date(as_of: str) -> date:
    """IST calendar date of the decision time."""
    return decision_dt(as_of).astimezone(IST).date()


def parse_date(x: Optional[str], default: date) -> date:
    return date.fromisoformat(x[:10]) if x else default


def register_all() -> dict[str, ToolDef]:
    # Importing the modules registers their tools.
    from . import tools_fundamentals, tools_market, tools_misc, tools_news, tools_quant  # noqa: F401

    return TOOLS
