"""Yahoo Finance fallback (§4.3 fallback 2) and reconciliation source, via the public chart API.

Calls query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS directly with
curl_cffi (browser TLS fingerprint) instead of the yfinance wrapper, whose
cookie/crumb dance fails behind HTTPS-inspecting proxies. Yahoo throttles
heavily, so this is used for a small sample per day, not the whole universe,
unless the primary source is down.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from .base import FetchResult, PermanentError, TokenBucket, utcnow, with_retries

IST = timezone(timedelta(hours=5, minutes=30))
CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"


def _dec(x) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        return Decimal(str(round(float(x), 4)))
    except (TypeError, ValueError):
        return None


def parse_chart(symbol: str, payload: dict, day: date) -> Optional[dict[str, Any]]:
    """Extract one trading day's OHLCV from a chart API response."""
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return None
    r = result[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    for i, ts in enumerate(r.get("timestamp") or []):
        if datetime.fromtimestamp(ts, IST).date() != day:
            continue
        close = _dec((q.get("close") or [None])[i])
        if close is None:
            return None
        vol = (q.get("volume") or [None])[i]
        return {"symbol": symbol, "trade_date": day, "open": _dec((q.get("open") or [None])[i]),
                "high": _dec((q.get("high") or [None])[i]), "low": _dec((q.get("low") or [None])[i]),
                "close": close, "volume": int(vol) if vol is not None else None}
    return None


class YahooFinance:
    name = "yfinance"

    def __init__(self, fetch_chart: Optional[Callable[[str, int, int], dict]] = None,
                 bucket: Optional[TokenBucket] = None):
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._fetch = fetch_chart or self._live_fetch
        self._session = None

    def _live_fetch(self, ticker: str, period1: int, period2: int) -> dict:
        if self._session is None:
            from curl_cffi import requests as creq

            from core.net import ca_bundle_path

            self._session = creq.Session(impersonate="chrome", verify=ca_bundle_path())
        r = self._session.get(CHART.format(ticker=ticker),
                              params={"period1": period1, "period2": period2, "interval": "1d"}, timeout=15)
        if r.status_code == 404:
            raise PermanentError(f"{ticker}: not found")
        r.raise_for_status()
        return r.json()

    def daily(self, symbols: list[str], day: date) -> FetchResult:
        start = int(datetime.combine(day - timedelta(days=3), time(), IST).timestamp())
        end = int(datetime.combine(day + timedelta(days=1), time(), IST).timestamp())
        rows, warnings = [], []
        for sym in symbols:
            def go(sym=sym):
                self.bucket.acquire()
                return self._fetch(f"{sym}.NS", start, end)
            try:
                row = parse_chart(sym, with_retries(go, attempts=2), day)
            except Exception as e:
                warnings.append(f"{sym}: {type(e).__name__}")
                continue
            if row:
                rows.append(row)
        return FetchResult(rows, self.name, utcnow(), warnings=warnings)
