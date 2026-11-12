"""Point-in-time price loading.

Prices are filtered by trade_date <= decision date. (Backfilled rows carry a late
ingested_at, so filtering EOD prices by ingested_at would hide history that was
public at the time; for news and fundamentals, publication time governs instead.)
OHLC are rescaled by adj_close/close so splits and bonuses don't look like crashes.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m


def load_bars(s: Session, instrument_ids: list[int], end: date, lookback_days: int = 420) -> dict[int, pd.DataFrame]:
    if not instrument_ids:
        return {}
    start = end - timedelta(days=lookback_days)
    q = select(m.PriceDaily.instrument_id, m.PriceDaily.trade_date, m.PriceDaily.open, m.PriceDaily.high,
               m.PriceDaily.low, m.PriceDaily.close, m.PriceDaily.adj_close, m.PriceDaily.volume,
               m.PriceDaily.traded_value, m.PriceDaily.delivery_pct).where(
        m.PriceDaily.instrument_id.in_(instrument_ids), m.PriceDaily.trade_date >= start,
        m.PriceDaily.trade_date <= end)
    df = pd.DataFrame(s.execute(q).all(), columns=["iid", "date", "open", "high", "low", "close", "adj_close",
                                                     "volume", "traded_value", "delivery_pct"])
    if df.empty:
        return {}
    for c in ("open", "high", "low", "close", "adj_close", "traded_value", "delivery_pct"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype(float)
    factor = (df["adj_close"] / df["close"]).fillna(1.0)
    for c in ("open", "high", "low"):
        df[c] = df[c] * factor
    df["close"] = df["adj_close"].fillna(df["close"])
    out = {}
    for iid, g in df.groupby("iid"):
        out[int(iid)] = g.drop(columns=["iid", "adj_close"]).set_index("date").sort_index()
    return out


def load_index(s: Session, index_code: str, end: date, lookback_days: int = 420) -> pd.Series:
    q = select(m.IndexPriceDaily.trade_date, m.IndexPriceDaily.close).where(
        m.IndexPriceDaily.index_code == index_code, m.IndexPriceDaily.trade_date <= end,
        m.IndexPriceDaily.trade_date >= end - timedelta(days=lookback_days)).order_by(m.IndexPriceDaily.trade_date)
    rows = s.execute(q).all()
    return pd.Series([float(c) for _, c in rows], index=[d for d, _ in rows], name=index_code, dtype=float)
