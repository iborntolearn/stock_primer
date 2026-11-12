"""Technical indicators on one instrument's daily bars.

Input: DataFrame indexed by date, columns open/high/low/close/volume/traded_value
(already split/bonus adjusted). All functions use only data up to each row (no look-ahead).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(down != 0, 100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's average true range."""
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def breakout(df: pd.DataFrame, n: int) -> pd.Series:
    """Close above the highest high of the previous n sessions."""
    return df["close"] > df["high"].shift(1).rolling(n, min_periods=n).max()


def volume_ratio(df: pd.DataFrame, n: int = 20) -> pd.Series:
    return df["volume"] / df["volume"].shift(1).rolling(n, min_periods=n).mean()


def ret(close: pd.Series, n: int, skip: int = 0) -> pd.Series:
    """n-session return, optionally skipping the most recent `skip` sessions (12-1 momentum)."""
    return close.shift(skip) / close.shift(n) - 1


def slope(s: pd.Series, n: int = 20) -> pd.Series:
    """Fractional change of a series over n sessions (used on moving averages)."""
    return s / s.shift(n) - 1


def latest(df: pd.DataFrame) -> dict[str, float | bool | None]:
    """The indicator snapshot exposed by quant-mcp.compute_indicators (last row)."""
    if df.empty:
        return {}
    c = df["close"]
    a = atr(df)
    s20, s50, s200 = sma(c, 20), sma(c, 50), sma(c, 200)
    hi52 = df["high"].rolling(252, min_periods=120).max()
    lo52 = df["low"].rolling(252, min_periods=120).min()
    vals = {
        "close": c.iloc[-1], "RSI14": rsi(c).iloc[-1], "ATR14": a.iloc[-1],
        "ATR_PCT": a.iloc[-1] / c.iloc[-1] * 100 if c.iloc[-1] else None,
        "SMA20": s20.iloc[-1], "SMA50": s50.iloc[-1], "SMA200": s200.iloc[-1],
        "SMA50_SLOPE20": slope(s50).iloc[-1], "SMA200_SLOPE20": slope(s200).iloc[-1],
        "VOL_RATIO20": volume_ratio(df).iloc[-1], "BREAKOUT20": bool(breakout(df, 20).iloc[-1]),
        "BREAKOUT55": bool(breakout(df, 55).iloc[-1]), "HI52": hi52.iloc[-1], "LO52": lo52.iloc[-1],
        "RET_1M": ret(c, 21).iloc[-1], "RET_3M": ret(c, 63).iloc[-1],
        "ADV20_VALUE": df["traded_value"].rolling(20, min_periods=10).mean().iloc[-1],
        "SWING_LOW10": df["low"].rolling(10, min_periods=5).min().iloc[-1],
        "SWING_LOW20": df["low"].rolling(20, min_periods=10).min().iloc[-1],
        "bars": len(df),
    }
    out: dict[str, float | bool | None] = {}
    for k, v in vals.items():
        if isinstance(v, (bool, np.bool_)):
            out[k] = bool(v)
        elif v is None or (isinstance(v, float) and np.isnan(v)) or pd.isna(v):
            out[k] = None
        else:
            out[k] = round(float(v), 4)
    return out
