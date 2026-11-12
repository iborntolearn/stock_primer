"""End-of-day ingestion (§4.2 18:00 eod_ingest, §4.3).

1. equity prices: NSE bhavcopy, else yfinance for the universe (fallback)
2. index closes (Nifty indices, sector indices, India VIX)
3. corporate actions for a trailing window
4. split/bonus-adjusted closes
5. reconciliation of a sample (+ any given candidates) against yfinance;
   disagreements raise data_anomaly HITL tasks and exclude the stock for the day
Every step records source health; the job is idempotent per trading date.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m
from core.settings import Settings, get_settings

from .adapters.base import PermanentError, RetryableError
from .adapters.corporate_actions import NseCorporateActions, share_multiplier
from .adapters.nse import NseArchives
from .adapters.yahoo import YahooFinance
from .reference import is_trading_day, universe_members, upsert_instrument

log = logging.getLogger(__name__)
PRIMARY = "nse_bhavcopy"


@dataclass
class IngestReport:
    day: date
    trading_day: bool = True
    equity_source: Optional[str] = None
    equity_rows: int = 0
    index_rows: int = 0
    corporate_actions: int = 0
    adjusted_instruments: int = 0
    anomalies: list[dict] = field(default_factory=list)
    reconciled: bool = False
    warnings: list[str] = field(default_factory=list)


class ReconciliationUnavailable(Exception):
    pass


def _health(s: Session, source: str, ok: bool, latency_ms: int, note: str = "", last_data: Optional[date] = None):
    s.merge(m.SourceHealth(source=source, checked_at=datetime.now(timezone.utc), ok=ok, latency_ms=latency_ms,
                           last_data_at=datetime.combine(last_data, datetime.min.time(), timezone.utc) if last_data else None,
                           note=note[:500]))


def load_equity(s: Session, rows: list[dict[str, Any]], source: str) -> int:
    """Upsert prices. A fallback source never overwrites a primary-source row."""
    n = 0
    for r in rows:
        iid = upsert_instrument(s, r["symbol"], name=r.get("name"), isin=r.get("isin"))
        existing = s.get(m.PriceDaily, (iid, r["trade_date"]))
        if existing is not None and existing.source == PRIMARY and source != PRIMARY:
            continue
        s.merge(m.PriceDaily(
            instrument_id=iid, trade_date=r["trade_date"], open=r.get("open"), high=r.get("high"), low=r.get("low"),
            close=r.get("close"), prev_close=r.get("prev_close"), volume=r.get("volume"),
            traded_value=r.get("traded_value"), delivery_pct=r.get("delivery_pct"), source=source,
            ingested_at=datetime.now(timezone.utc)))
        n += 1
    return n


def load_indices(s: Session, rows: list[dict[str, Any]], source: str = "nse_index_bhavcopy") -> int:
    for r in rows:
        s.merge(m.IndexPriceDaily(index_code=r["index_code"], trade_date=r["trade_date"], open=r.get("open"),
                                  high=r.get("high"), low=r.get("low"), close=r["close"], pe=r.get("pe"),
                                  source=source, ingested_at=datetime.now(timezone.utc)))
    return len(rows)


def load_corporate_actions(s: Session, actions: list[dict[str, Any]], source: str) -> set[int]:
    """Insert new actions; return instruments whose share count changes (need re-adjustment)."""
    touched: set[int] = set()
    for a in actions:
        iid = upsert_instrument(s, a["symbol"], isin=a.get("isin"))
        exists = s.scalar(select(m.CorporateAction.id).where(
            m.CorporateAction.instrument_id == iid, m.CorporateAction.action_type == a["action_type"],
            m.CorporateAction.ex_date == a["ex_date"], m.CorporateAction.source == source))
        if exists:
            continue
        s.add(m.CorporateAction(instrument_id=iid, action_type=a["action_type"], ex_date=a["ex_date"],
                                ratio_num=a.get("ratio_num"), ratio_den=a.get("ratio_den"),
                                amount_per_share=a.get("amount_per_share"),
                                details={**(a.get("details") or {}), "subject": a.get("raw_subject")}, source=source))
        if a["action_type"] in ("split", "bonus"):
            touched.add(iid)
    return touched


def recompute_adjusted(s: Session, instrument_ids: Optional[set[int]] = None) -> int:
    """adj_close = close / product(share multipliers of split/bonus with ex_date > trade_date)."""
    q = select(m.CorporateAction).where(m.CorporateAction.action_type.in_(("split", "bonus")))
    actions: dict[int, list[tuple[date, Decimal]]] = {}
    for a in s.scalars(q):
        actions.setdefault(a.instrument_id, []).append(
            (a.ex_date, share_multiplier(a.action_type, a.ratio_num, a.ratio_den)))
    targets = instrument_ids if instrument_ids is not None else None
    price_q = select(m.PriceDaily)
    if targets is not None:
        if not targets:
            return 0
        price_q = price_q.where(m.PriceDaily.instrument_id.in_(targets))
    n = 0
    for p in s.scalars(price_q):
        factor = Decimal(1)
        for ex, mult in actions.get(p.instrument_id, []):
            if ex > p.trade_date:
                factor *= mult
        adj = (p.close / factor).quantize(Decimal("0.0001")) if p.close is not None else None
        if p.adj_close != adj:
            p.adj_close = adj
            n += 1
    return n


def reconcile(s: Session, day: date, yahoo: YahooFinance, symbols: list[str], settings: Settings,
              run_id: Optional[int] = None) -> list[dict]:
    """Compare stored primary-source closes with yfinance; raise data_anomaly tasks on disagreement."""
    if not symbols:
        return []
    try:
        theirs = {r["symbol"]: r for r in yahoo.daily(symbols, day).records}
    except (RetryableError, PermanentError) as e:
        _health(s, yahoo.name, False, 0, str(e))
        raise ReconciliationUnavailable(str(e)) from e
    if len(theirs) < max(1, len(symbols) // 2):
        note = f"returned {len(theirs)}/{len(symbols)} symbols"
        _health(s, yahoo.name, False, 0, note)
        raise ReconciliationUnavailable(note)
    _health(s, yahoo.name, True, 0, last_data=day)
    anomalies = []
    for sym in symbols:
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == sym))
        ours = s.get(m.PriceDaily, (iid, day)) if iid else None
        other = theirs.get(sym)
        if ours is None or other is None or ours.close is None:
            continue
        close_dev = abs(float(ours.close) - float(other["close"])) / float(ours.close)
        vol_dev = (abs(ours.volume - other["volume"]) / ours.volume
                   if ours.volume and other.get("volume") else 0.0)
        if close_dev > settings.reconcile_close_tolerance or vol_dev > settings.reconcile_volume_tolerance:
            a = {"symbol": sym, "day": day.isoformat(), "ours": float(ours.close), "theirs": float(other["close"]),
                 "close_dev": round(close_dev, 4), "volume_dev": round(vol_dev, 4)}
            anomalies.append(a)
            s.add(m.HitlTask(task_type="data_anomaly", subject_type="instrument", subject_id=iid, priority=2,
                             payload={"check": "reconciliation", **a}, ai_recommendation="exclude",
                             reason=f"{sym}: close differs {close_dev:.2%} / volume {vol_dev:.0%} vs yfinance",
                             default_on_expiry="exclude_instrument"))
    return anomalies


def run_eod_ingest(factory: sessionmaker[Session], day: date, *, nse: NseArchives, yahoo: YahooFinance,
                   corp: NseCorporateActions, settings: Optional[Settings] = None,
                   reconcile_symbols: Optional[list[str]] = None, ca_lookback_days: int = 30,
                   rng: Optional[random.Random] = None) -> IngestReport:
    settings = settings or get_settings()
    rep = IngestReport(day)
    with factory() as s, s.begin():
        if not is_trading_day(s, day):
            rep.trading_day = False
            return rep
        members = universe_members(s, settings.universe, day)

    # 1. equity prices
    t0 = time.perf_counter()
    try:
        res = nse.equity(day)
        with factory() as s, s.begin():
            rep.equity_rows = load_equity(s, res.records, PRIMARY)
            _health(s, PRIMARY, True, int((time.perf_counter() - t0) * 1000), "; ".join(res.warnings), day)
        rep.equity_source = PRIMARY
        rep.warnings += res.warnings
    except PermanentError as e:
        # NSE publishes no bhavcopy on exchange holidays. For past dates that is how we learn about holidays
        # the holiday API no longer lists (it only covers the current year).
        if day < datetime.now(timezone.utc).date():
            with factory() as s, s.begin():
                s.merge(m.TradingCalendar(cal_date=day, is_trading_day=False, note="no bhavcopy published (holiday)"))
            rep.trading_day = False
            return rep
        rep.warnings.append(f"primary equity source failed: {e}")
        with factory() as s, s.begin():
            _health(s, PRIMARY, False, int((time.perf_counter() - t0) * 1000), str(e))
    except RetryableError as e:
        rep.warnings.append(f"primary equity source failed: {e}")
        with factory() as s, s.begin():
            _health(s, PRIMARY, False, int((time.perf_counter() - t0) * 1000), str(e))
        if members:
            try:
                fb = yahoo.daily(sorted(members.values()), day)
                with factory() as s, s.begin():
                    rep.equity_rows = load_equity(s, fb.records, yahoo.name)
                rep.equity_source = yahoo.name
            except (RetryableError, PermanentError) as e2:
                rep.warnings.append(f"fallback equity source failed: {e2}")

    # 2. indices
    try:
        idx = nse.indices(day)
        with factory() as s, s.begin():
            rep.index_rows = load_indices(s, idx.records)
            _health(s, "nse_index_bhavcopy", True, 0, last_data=day)
    except (RetryableError, PermanentError) as e:
        rep.warnings.append(f"index bhavcopy failed: {e}")
        with factory() as s, s.begin():
            _health(s, "nse_index_bhavcopy", False, 0, str(e))

    # 3-4. corporate actions + adjustment
    try:
        ca = corp.fetch(day - timedelta(days=ca_lookback_days), day + timedelta(days=7))
        with factory() as s, s.begin():
            touched = load_corporate_actions(s, ca.records, corp.name)
            rep.corporate_actions = len(ca.records)
            _health(s, corp.name, True, 0, last_data=day)
        with factory() as s, s.begin():
            rep.adjusted_instruments = len(touched)
            recompute_adjusted(s, touched)
            # New rows for today need adj_close too.
            todays = set(s.scalars(select(m.PriceDaily.instrument_id).where(m.PriceDaily.trade_date == day,
                                                                             m.PriceDaily.adj_close.is_(None))))
            recompute_adjusted(s, todays)
    except (RetryableError, PermanentError) as e:
        rep.warnings.append(f"corporate actions failed: {e}")
        with factory() as s, s.begin():
            _health(s, corp.name, False, 0, str(e))
            todays = set(s.scalars(select(m.PriceDaily.instrument_id).where(m.PriceDaily.trade_date == day)))
            recompute_adjusted(s, todays)

    # 5. reconciliation (only meaningful when the primary source supplied the data)
    if rep.equity_source == PRIMARY and members:
        rng = rng or random.Random(day.toordinal())
        pool = sorted(members.values())
        sample = set(rng.sample(pool, min(settings.reconcile_sample_size, len(pool))))
        sample |= set(reconcile_symbols or [])
        try:
            with factory() as s, s.begin():
                rep.anomalies = reconcile(s, day, yahoo, sorted(sample), settings)
            rep.reconciled = True
        except ReconciliationUnavailable as e:
            with factory() as s, s.begin():
                _health(s, yahoo.name, False, 0, str(e))
            rep.warnings.append(f"reconciliation skipped: {yahoo.name} {e}")
    return rep
