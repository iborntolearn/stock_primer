"""Reference data: trading calendar, universe membership (point-in-time), seed rows."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m
from core.store import SqlStore

# Statutory/broker rates change; the simulator must not trust these until verified (§4.6).
COST_CONFIG_V1 = {
    "verify_before_use": True,
    "brokerage_pct": 0.0, "brokerage_max_per_order": 20.0,
    "stt_delivery_pct": 0.1,                 # buy and sell, delivery
    "exchange_txn_pct": 0.00297,             # NSE cash
    "sebi_fee_per_crore": 10.0,
    "gst_pct": 18.0,                         # on brokerage + exchange + SEBI fee
    "stamp_duty_buy_pct": 0.015,
    "dp_charge_per_sell_scrip": 15.93,
    "slippage_bps_base": 5, "slippage_bps_per_pct_adv": 2,
}

MACRO_FACTORS = [
    ("crude_up", "Brent crude rising", "BRENT"), ("usdinr_up", "Rupee weakening", "USDINR"),
    ("us10y_up", "US 10Y yield rising", "DGS10"), ("gold_up", "Gold rising", "GOLD"),
    ("repo_up", "RBI repo rate rising", "REPO"), ("monsoon_weak", "Weak monsoon", None),
]


def sync_calendar(s: Session, start: date, end: date, holidays: Iterable[dict[str, Any]]) -> int:
    """Weekdays minus exchange holidays are trading days."""
    hol = {h["date"]: h.get("note") for h in holidays}
    n = 0
    d = start
    while d <= end:
        trading = d.weekday() < 5 and d not in hol
        note = hol.get(d) or ("weekend" if d.weekday() >= 5 else None)
        s.merge(m.TradingCalendar(cal_date=d, is_trading_day=trading, note=note))
        d += timedelta(days=1)
        n += 1
    return n


def is_trading_day(s: Session, d: date) -> bool:
    row = s.get(m.TradingCalendar, d)
    if row is None:
        return d.weekday() < 5  # calendar not loaded: assume weekdays trade, the gate will catch missing data
    return row.is_trading_day


def previous_trading_day(s: Session, d: date) -> date:
    p = d - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(s, p):
            return p
        p -= timedelta(days=1)
    return p


def upsert_instrument(s: Session, symbol: str, *, name: str | None = None, isin: str | None = None,
                      industry: str | None = None) -> int:
    iid = SqlStore.instrument_id(s, symbol)
    inst = s.get(m.Instrument, iid)
    if name and inst.name == symbol:
        inst.name = name
    if isin and not inst.isin:
        inst.isin = isin
    if industry:
        inst.industry = industry
        if inst.sector_id is None:
            inst.sector_id = SqlStore.sector_id(s, industry)
    return iid


def sync_universe(s: Session, universe: str, constituents: list[dict[str, Any]], as_of: date) -> dict[str, int]:
    """Open membership for new names, close it for names that left. History starts at the first snapshot."""
    current = {r.instrument_id: r for r in s.scalars(select(m.UniverseMembership).where(
        m.UniverseMembership.universe == universe, m.UniverseMembership.valid_to.is_(None)))}
    seen = set()
    added = 0
    for c in constituents:
        iid = upsert_instrument(s, c["symbol"], name=c["name"], isin=c["isin"], industry=c["industry"])
        seen.add(iid)
        if iid not in current:
            s.add(m.UniverseMembership(instrument_id=iid, universe=universe, valid_from=as_of))
            added += 1
    removed = 0
    for iid, row in current.items():
        if iid not in seen:
            row.valid_to = as_of - timedelta(days=1)
            removed += 1
    return {"members": len(seen), "added": added, "removed": removed}


def universe_members(s: Session, universe: str, as_of: date) -> dict[int, str]:
    q = (select(m.Instrument.id, m.Instrument.symbol)
         .join(m.UniverseMembership, m.UniverseMembership.instrument_id == m.Instrument.id)
         .where(m.UniverseMembership.universe == universe, m.UniverseMembership.valid_from <= as_of,
                (m.UniverseMembership.valid_to.is_(None)) | (m.UniverseMembership.valid_to >= as_of)))
    return {iid: sym for iid, sym in s.execute(q)}


def seed(s: Session) -> None:
    if s.get(m.CostConfig, 1) is None:
        s.add(m.CostConfig(version=1, effective_from=date(2024, 10, 1), rates=COST_CONFIG_V1))
    for code, name, series in MACRO_FACTORS:
        if s.scalar(select(m.MacroFactor.id).where(m.MacroFactor.code == code)) is None:
            s.add(m.MacroFactor(code=code, name=name, series_id=series))
    s.flush()
    from .sensitivity_seed import seed_sensitivity_map

    seed_sensitivity_map(s)
    if s.scalar(select(m.ConfigVersion.version).limit(1)) is None:
        from orchestrator.config import Config

        s.add(m.ConfigVersion(config=Config().model_dump(mode="json"), created_by="seed", approved=True))
