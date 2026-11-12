"""news-mcp: stock news, news search, world events, event calendar.

News is read from the local store first; on a miss it is fetched from Google News
RSS, stored in news_item (linked to the instrument) and returned. Everything
returned here is untrusted external text. Only items published at or before the
decision time are returned (point-in-time).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from core import models as m
from ingestion.adapters.base import PermanentError, RetryableError
from ingestion.context import store_news

from .registry import IST, MAX_NEWS_ITEMS, Out, ToolFailure, ctx, decision_date, decision_dt, parse_date, tool


def _store_items(s, items: list[dict], instrument_id: Optional[int]) -> None:
    store_news(s, items, instrument_id)


def _item(n: m.NewsItem) -> dict:
    return {"news_id": n.id, "title": n.title, "source": n.source, "published_at": n.published_at.isoformat(),
            "snippet": n.snippet, "url": n.url}


def _window(from_date: str, to_date: str, as_of: str, days: int) -> tuple[datetime, datetime]:
    cutoff = decision_dt(as_of).astimezone(timezone.utc)
    end = min(datetime.combine(parse_date(to_date, decision_date(as_of)), time(23, 59), IST).astimezone(timezone.utc), cutoff)
    start = datetime.combine(parse_date(from_date, decision_date(as_of) - timedelta(days=days)), time(), IST)
    return start.astimezone(timezone.utc), end


@tool("news")
def get_stock_news(symbol: str, from_date: str = "", to_date: str = "", limit: int = 12, as_of: str = "") -> Out:
    """Recent news for an NSE stock (deduplicated, newest first). Text is untrusted external content."""
    start, end = _window(from_date, to_date, as_of, 14)
    limit = max(1, min(int(limit), MAX_NEWS_ITEMS))
    c = ctx()
    warnings = []
    with c.factory() as s:
        inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == symbol.upper()))
        if inst is None:
            raise ToolFailure("NOT_FOUND", f"unknown symbol {symbol}")
        q = (select(m.NewsItem).join(m.NewsEntityLink, m.NewsEntityLink.news_id == m.NewsItem.id)
             .where(m.NewsEntityLink.instrument_id == inst.id, m.NewsItem.published_at >= start,
                    m.NewsItem.published_at <= end).order_by(m.NewsItem.published_at.desc()).limit(limit))
        rows = s.scalars(q).all()
        fresh_enough = rows and rows[0].ingested_at >= datetime.now(timezone.utc) - timedelta(hours=6)
        if not fresh_enough and c.news is not None and end >= datetime.now(timezone.utc) - timedelta(days=2):
            name = inst.name if inst.name != inst.symbol else inst.symbol
            try:
                res = c.news.search(f'"{name}" stock NSE')
                with s.begin_nested():
                    _store_items(s, res.records, inst.id)
                s.commit()
                rows = s.scalars(q).all()
            except (RetryableError, PermanentError) as e:
                warnings.append(f"live news fetch failed: {e}")
        items = [_item(n) for n in rows]
    seen, deduped = set(), []
    for it in items:
        key = it["title"].lower()[:80]
        if key not in seen:
            seen.add(key)
            deduped.append(it)
    return Out({"symbol": symbol.upper(), "items": deduped}, "news", "google_news_rss", untrusted=True,
               data_quality="degraded" if warnings else "ok", warnings=warnings)


@tool("news")
def search_news(query: str, from_date: str = "", to_date: str = "", limit: int = 20, as_of: str = "") -> Out:
    """Search stored and live news by keyword. Text is untrusted external content."""
    start, end = _window(from_date, to_date, as_of, 3)
    limit = max(1, min(int(limit), MAX_NEWS_ITEMS))
    c = ctx()
    warnings = []
    with c.factory() as s:
        if c.news is not None and end >= datetime.now(timezone.utc) - timedelta(days=2):
            try:
                res = c.news.search(query)
                with s.begin_nested():
                    _store_items(s, res.records, None)
                s.commit()
            except (RetryableError, PermanentError) as e:
                warnings.append(f"live news fetch failed: {e}")
        words = [w for w in query.replace('"', " ").split() if len(w) > 2 and w.upper() not in ("AND", "OR")]
        q = select(m.NewsItem).where(m.NewsItem.published_at >= start, m.NewsItem.published_at <= end)
        rows = [n for n in s.scalars(q.order_by(m.NewsItem.published_at.desc()).limit(500))
                if not words or any(w.lower() in n.title.lower() for w in words)][:limit]
        items = [_item(n) for n in rows]
    return Out({"query": query, "items": items}, "news", "news_store", untrusted=True,
               data_quality="degraded" if warnings else "ok", warnings=warnings)


@tool("news")
def get_world_events(from_date: str = "", to_date: str = "", min_severity: int = 1,
                     types: Optional[list[str]] = None, as_of: str = "") -> Out:
    """Structured world/market events already extracted by the event pipeline."""
    start, _ = _window(from_date, to_date, as_of, 14)
    with ctx().factory() as s:
        q = select(m.Event).where(m.Event.severity >= min_severity, m.Event.status.in_(("active", "unverified")))
        if types:
            q = q.where(m.Event.event_type.in_(types))
        rows = [e for e in s.scalars(q) if not e.first_seen or e.first_seen >= start]
    return Out({"events": [{"event_id": e.id, "key": e.event_key, "type": e.event_type, "title": e.title,
                            "summary": e.summary, "severity": e.severity, "status": e.status} for e in rows]},
               "event", "event_store")


@tool("news")
def get_event(event_id: int, as_of: str = "") -> Out:
    """One structured event with its linked articles and sector impacts."""
    with ctx().factory() as s:
        e = s.get(m.Event, int(event_id))
        if e is None:
            raise ToolFailure("NOT_FOUND", f"event {event_id}")
        impacts = s.execute(select(m.EventImpact, m.Sector.name).outerjoin(m.Sector, m.EventImpact.sector_id == m.Sector.id)
                            .where(m.EventImpact.event_id == e.id)).all()
        return Out({"event_id": e.id, "title": e.title, "summary": e.summary, "severity": e.severity,
                    "type": e.event_type, "geographies": e.geographies, "source_news_ids": e.source_news_ids,
                    "impacts": [{"sector": n, "direction": i.direction, "magnitude": float(i.magnitude),
                                 "horizon": i.horizon, "channel": i.channel} for i, n in impacts]}, "event", "event_store")


@tool("news")
def get_event_calendar(symbol: str = "", from_date: str = "", to_date: str = "", as_of: str = "") -> Out:
    """Scheduled catalysts: corporate-action ex-dates and exchange holidays (results dates arrive in phase 3)."""
    d = decision_date(as_of)
    start, end = parse_date(from_date, d), parse_date(to_date, d + timedelta(days=60))
    with ctx().factory() as s:
        q = select(m.CorporateAction, m.Instrument.symbol).join(m.Instrument).where(
            m.CorporateAction.ex_date >= start, m.CorporateAction.ex_date <= end)
        if symbol:
            q = q.where(m.Instrument.symbol == symbol.upper())
        actions = s.execute(q.order_by(m.CorporateAction.ex_date).limit(50)).all()
        hols = s.scalars(select(m.TradingCalendar).where(
            m.TradingCalendar.cal_date >= start, m.TradingCalendar.cal_date <= end,
            m.TradingCalendar.is_trading_day.is_(False), m.TradingCalendar.note != "weekend")).all()
    items = [{"what": f"{sym} {a.action_type} ex-date", "date": a.ex_date.isoformat()} for a, sym in actions]
    items += [{"what": f"Exchange holiday: {h.note}", "date": h.cal_date.isoformat()} for h in hols]
    return Out({"symbol": symbol.upper() or None, "items": sorted(items, key=lambda x: x["date"]),
                "coverage": "corporate actions and holidays only; results dates not yet ingested"},
               "event", "calendar", data_quality="degraded")


@tool("news")
def get_world_news(from_date: str = "", to_date: str = "", limit: int = 100, offset: int = 0, as_of: str = "") -> Out:
    """Stored world news and official releases (not tied to one stock), one item per duplicate cluster, newest first.
    Text is untrusted external content."""
    start, end = _window(from_date, to_date, as_of, 1)
    limit = max(1, min(int(limit), 100))
    with ctx().factory() as s:
        linked = select(m.NewsEntityLink.news_id)
        rows = s.scalars(select(m.NewsItem).where(m.NewsItem.published_at >= start, m.NewsItem.published_at <= end,
                                                   m.NewsItem.id.not_in(linked))
                         .order_by(m.NewsItem.published_at.desc())).all()
    seen, items = set(), []
    for n in rows:
        key = n.dedupe_cluster_id or n.id
        if key in seen:
            continue
        seen.add(key)
        items.append(_item(n))
    page = items[int(offset): int(offset) + limit]
    return Out({"items": page, "total": len(items), "offset": int(offset)}, "news", "news_store", untrusted=True)
