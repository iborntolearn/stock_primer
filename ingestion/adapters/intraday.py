"""Delayed intraday quotes for stop-proximity alerts (Phase 5, optional).

Yahoo's chart API at 5-minute bars, typically 15+ minutes delayed for NSE. Good enough
to warn that a stop is close; never used for simulated fills, which stay end-of-day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .base import PermanentError, TokenBucket, with_retries
from .yahoo import CHART

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Quote:
    symbol: str
    last: float
    day_high: float
    day_low: float
    at: datetime


def parse_intraday(symbol: str, payload: dict) -> Optional[Quote]:
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return None
    r = result[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    ts = r.get("timestamp") or []
    bars = [(t, c, h, lo) for t, c, h, lo in zip(ts, q.get("close") or [], q.get("high") or [], q.get("low") or [])
            if c is not None]
    if not bars:
        return None
    day = datetime.fromtimestamp(bars[-1][0], IST).date()
    today = [b for b in bars if datetime.fromtimestamp(b[0], IST).date() == day]
    return Quote(symbol, float(today[-1][1]), max(float(b[2] if b[2] is not None else b[1]) for b in today),
                 min(float(b[3] if b[3] is not None else b[1]) for b in today), datetime.fromtimestamp(today[-1][0], IST))


class YahooIntraday:
    name = "yahoo_intraday"

    def __init__(self, fetch: Optional[Callable[[str], dict]] = None, bucket: Optional[TokenBucket] = None):
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._fetch = fetch or self._live
        self._session = None

    def _live(self, ticker: str) -> dict:
        if self._session is None:
            from curl_cffi import requests as creq

            from core.net import ca_bundle_path

            self._session = creq.Session(impersonate="chrome", verify=ca_bundle_path())
        r = self._session.get(CHART.format(ticker=ticker), params={"interval": "5m", "range": "1d"}, timeout=15)
        if r.status_code == 404:
            raise PermanentError(f"{ticker}: not found")
        r.raise_for_status()
        return r.json()

    def quotes(self, symbols: list[str]) -> tuple[dict[str, Quote], list[str]]:
        out, warnings = {}, []
        for sym in symbols:
            def go(sym=sym) -> Any:
                self.bucket.acquire()
                return self._fetch(f"{sym}.NS")
            try:
                q = parse_intraday(sym, with_retries(go, attempts=2))
            except Exception as e:
                warnings.append(f"{sym}: {type(e).__name__}")
                continue
            if q:
                out[sym] = q
        return out, warnings
