"""Market-context ingestion (§4.2 06:30 global_overnight_ingest): macro series, FII/DII flows,
results calendar, world news and official releases.

News dedupe: the doc's pgvector embeddings need Postgres and an embedding model, so
for now duplicates are clustered by title-token overlap (Jaccard >= 0.6 within 48 h).
Every source is independent: one failing adds a warning and marks source_health,
the rest still load.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m

from .eod import _health
from .reference import upsert_instrument

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "as", "at", "by", "with", "is", "are", "its",
         "from", "after", "over", "amid", "says", "said", "new", "will"}


def title_tokens(title: str) -> set[str]:
    return {w for w in _WORD.findall(title.lower()) if w not in _STOP and len(w) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def store_news(s: Session, items: list[dict], instrument_id: Optional[int] = None, link_method: str = "query",
               cluster_window_h: int = 48, threshold: float = 0.6) -> int:
    """Insert new items (by URL), link to an instrument, and assign dedupe clusters. Returns new rows."""
    new = 0
    recent: Optional[list[tuple[int, set[str], int]]] = None
    for it in items:
        row = s.scalar(select(m.NewsItem).where(m.NewsItem.url == it["url"]))
        if row is None:
            if recent is None:
                since = datetime.now(timezone.utc) - timedelta(hours=cluster_window_h)
                recent = [(n.id, title_tokens(n.title), n.dedupe_cluster_id or n.id)
                          for n in s.scalars(select(m.NewsItem).where(m.NewsItem.published_at >= since))]
            toks = title_tokens(it["title"])
            cluster = next((c for _, t, c in recent if jaccard(toks, t) >= threshold), None)
            row = m.NewsItem(url=it["url"], url_hash=it["url_hash"], source=it["source"], title=it["title"],
                             snippet=it.get("snippet"), published_at=it["published_at"], language=it.get("language", "en"))
            s.add(row)
            s.flush()
            row.dedupe_cluster_id = cluster or row.id
            recent.append((row.id, toks, row.dedupe_cluster_id))
            new += 1
        if instrument_id and s.get(m.NewsEntityLink, (row.id, instrument_id)) is None:
            s.add(m.NewsEntityLink(news_id=row.id, instrument_id=instrument_id, method=link_method))
    return new


def load_macro(s: Session, rows: list[dict], source: str = "yahoo") -> int:
    for r in rows:
        s.merge(m.MacroSeries(series_id=r["series_id"], obs_date=r["obs_date"], value=r["value"], source=source))
    return len(rows)


def load_flows(s: Session, rows: list[dict], source: str = "nse_fiidii") -> int:
    for r in rows:
        s.merge(m.FlowsDaily(trade_date=r["trade_date"], fii_net=r["fii_net"], dii_net=r["dii_net"], source=source))
    return len(rows)


def load_results_calendar(s: Session, rows: list[dict], source: str = "nse_board_meetings") -> int:
    """Results board meetings become 'results' corporate actions (catalysts in the event calendar)."""
    n = 0
    for r in rows:
        if not r["is_results"]:
            continue
        iid = upsert_instrument(s, r["symbol"])
        exists = s.scalar(select(m.CorporateAction.id).where(
            m.CorporateAction.instrument_id == iid, m.CorporateAction.action_type == "results",
            m.CorporateAction.ex_date == r["date"]))
        if exists is None:
            s.add(m.CorporateAction(instrument_id=iid, action_type="results", ex_date=r["date"],
                                    details={"subject": r["purpose"], "desc": r["desc"]}, source=source))
            n += 1
    return n


@dataclass
class ContextReport:
    macro_rows: int = 0
    flows_rows: int = 0
    results_dates: int = 0
    world_news_new: int = 0
    official_new: int = 0
    warnings: list[str] = field(default_factory=list)


def run_context_ingest(f: sessionmaker[Session], today: date, *, macro=None, nse_extra=None, world=None,
                       macro_days: int = 15, calendar_days: int = 45) -> ContextReport:
    rep = ContextReport()

    def step(name: str, fn):
        try:
            fn()
            with f() as s, s.begin():
                _health(s, name, True, 0, last_data=today)
        except Exception as e:
            rep.warnings.append(f"{name}: {type(e).__name__}: {str(e)[:160]}")
            with f() as s, s.begin():
                _health(s, name, False, 0, str(e))

    if macro is not None:
        def do_macro():
            res = macro.fetch(today - timedelta(days=macro_days), today)
            rep.warnings.extend(res.warnings)
            with f() as s, s.begin():
                rep.macro_rows = load_macro(s, res.records)
        step("yahoo_macro", do_macro)
    if nse_extra is not None:
        def do_flows():
            with f() as s, s.begin():
                rep.flows_rows = load_flows(s, nse_extra.flows().records)
        step("nse_fiidii", do_flows)

        def do_calendar():
            res = nse_extra.board_meetings(today, today + timedelta(days=calendar_days))
            with f() as s, s.begin():
                rep.results_dates = load_results_calendar(s, res.records)
        step("nse_board_meetings", do_calendar)
    if world is not None:
        def do_world():
            res = world.themes()
            rep.warnings.extend(res.warnings)
            with f() as s, s.begin():
                rep.world_news_new = store_news(s, res.records)
        step("world_news", do_world)

        def do_official():
            res = world.official()
            rep.warnings.extend(res.warnings)
            with f() as s, s.begin():
                rep.official_new = store_news(s, res.records)
        step("official_rss", do_official)
    return rep
