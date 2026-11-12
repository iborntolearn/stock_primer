"""Macro series (§4.3 macro): Brent, USD/INR, gold, US 10Y yield, dollar index via Yahoo's chart API.

FRED is the doc's primary for US series but was unreachable from this network
(timeouts), so ^TNX (10Y yield x1) and DX-Y.NYB stand in. Each series keeps its
Yahoo ticker in macro_series.source so a later FRED adapter can replace it.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from .base import FetchResult, PermanentError, TokenBucket, utcnow, with_retries

SERIES = {"BRENT": "BZ=F", "USDINR": "USDINR=X", "GOLD": "GC=F", "US10Y": "^TNX", "DXY": "DX-Y.NYB"}
CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
UTC = timezone.utc


def parse_series(series_id: str, payload: dict) -> list[dict[str, Any]]:
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return []
    r = result[0]
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    tz = timezone(timedelta(seconds=(r.get("meta") or {}).get("gmtoffset", 0)))
    out, seen = [], set()
    for ts, c in zip(r.get("timestamp") or [], closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(ts, tz).date()    # the exchange's local trading date
        if d in seen:
            continue
        seen.add(d)
        out.append({"series_id": series_id, "obs_date": d, "value": Decimal(str(round(float(c), 4)))})
    return out


class YahooMacro:
    name = "yahoo_macro"

    def __init__(self, fetch: Optional[Callable[[str, int, int], dict]] = None, bucket: Optional[TokenBucket] = None):
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._fetch = fetch or self._live
        self._session = None

    def _live(self, ticker: str, p1: int, p2: int) -> dict:
        if self._session is None:
            from curl_cffi import requests as creq

            from core.net import ca_bundle_path

            self._session = creq.Session(impersonate="chrome", verify=ca_bundle_path())
        r = self._session.get(CHART.format(ticker=ticker), params={"period1": p1, "period2": p2, "interval": "1d"}, timeout=15)
        if r.status_code == 404:
            raise PermanentError(f"{ticker} not found")
        r.raise_for_status()
        return r.json()

    def fetch(self, start: date, end: date, series: Optional[list[str]] = None) -> FetchResult:
        p1 = int(datetime.combine(start, time(), UTC).timestamp())
        p2 = int(datetime.combine(end + timedelta(days=1), time(), UTC).timestamp())
        rows, warnings = [], []
        for sid in series or list(SERIES):
            def go(sid=sid):
                self.bucket.acquire()
                return self._fetch(SERIES[sid], p1, p2)
            try:
                rows += [r for r in parse_series(sid, with_retries(go, attempts=2)) if start <= r["obs_date"] <= end]
            except Exception as e:
                warnings.append(f"{sid}: {type(e).__name__}: {e}")
        return FetchResult(rows, self.name, utcnow(), warnings=warnings)
