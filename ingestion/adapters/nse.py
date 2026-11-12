"""NSE sources via jugaad-data: equity bhavcopy (primary EOD), full bhavcopy (delivery %),
index bhavcopy (Nifty indices, sector indices, India VIX) and the trading-holiday list.

Parsers are pure functions over the raw CSV text so they can be tested against
recorded files. Network access goes through the token bucket, disk cache and
retries in `base`.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from .base import DiskCache, FetchResult, Health, PermanentError, RetryableError, TokenBucket, utcnow, with_retries

EQUITY_SERIES = {"EQ", "BE", "BZ"}


def _d(x: Optional[str]) -> Optional[Decimal]:
    if x is None:
        return None
    x = x.strip()
    if x in ("", "-", "NA"):
        return None
    try:
        return Decimal(x)
    except InvalidOperation:
        return None


def _i(x: Optional[str]) -> Optional[int]:
    v = _d(x)
    return None if v is None else int(v)


def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.strip()), skipinitialspace=True)
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]


def parse_equity_bhavcopy(text: str) -> list[dict[str, Any]]:
    """UDiff format (from 2024-07-08) or the legacy cm*bhav.csv format -> normalised rows."""
    rows = _rows(text)
    if not rows:
        return []
    out = []
    if "TckrSymb" in rows[0]:
        for r in rows:
            if r.get("Sgmt") != "CM" or r.get("SctySrs") not in EQUITY_SERIES:
                continue
            out.append({
                "symbol": r["TckrSymb"], "series": r["SctySrs"], "isin": r.get("ISIN") or None,
                "name": r.get("FinInstrmNm") or r["TckrSymb"],
                "trade_date": date.fromisoformat(r["TradDt"]),
                "open": _d(r["OpnPric"]), "high": _d(r["HghPric"]), "low": _d(r["LwPric"]),
                "close": _d(r["ClsPric"]), "prev_close": _d(r.get("PrvsClsgPric")),
                "volume": _i(r["TtlTradgVol"]), "traded_value": _d(r["TtlTrfVal"]),
            })
    elif "SYMBOL" in rows[0] and "TOTTRDQTY" in rows[0]:
        for r in rows:
            if r.get("SERIES") not in EQUITY_SERIES:
                continue
            out.append({
                "symbol": r["SYMBOL"], "series": r["SERIES"], "isin": r.get("ISIN") or None, "name": r["SYMBOL"],
                "trade_date": datetime.strptime(r["TIMESTAMP"], "%d-%b-%Y").date(),
                "open": _d(r["OPEN"]), "high": _d(r["HIGH"]), "low": _d(r["LOW"]), "close": _d(r["CLOSE"]),
                "prev_close": _d(r.get("PREVCLOSE")), "volume": _i(r["TOTTRDQTY"]),
                "traded_value": _d(r["TOTTRDVAL"]),
            })
    else:
        raise PermanentError(f"unrecognised bhavcopy header: {list(rows[0])[:6]}")
    return out


def parse_full_bhavcopy(text: str) -> dict[str, Decimal]:
    """sec_bhavdata_full -> {symbol: delivery_pct} for equity series."""
    out = {}
    for r in _rows(text):
        if r.get("SERIES") in EQUITY_SERIES:
            pct = _d(r.get("DELIV_PER"))
            if pct is not None:
                out[r["SYMBOL"]] = pct
    return out


def parse_index_bhavcopy(text: str) -> list[dict[str, Any]]:
    out = []
    for r in _rows(text):
        close = _d(r.get("Closing Index Value"))
        if close is None:
            continue
        out.append({
            "index_code": r["Index Name"].upper(),
            "trade_date": datetime.strptime(r["Index Date"], "%d-%m-%Y").date(),
            "open": _d(r.get("Open Index Value")), "high": _d(r.get("High Index Value")),
            "low": _d(r.get("Low Index Value")), "close": close, "pe": _d(r.get("P/E")),
        })
    return out


def parse_holidays(payload: dict) -> list[dict[str, Any]]:
    """NSE holiday-master JSON -> [{date, note}] for the cash market (segment CM)."""
    out = []
    for h in payload.get("CM", []):
        d = datetime.strptime(h["tradingDate"], "%d-%b-%Y").date()
        out.append({"date": d, "note": h.get("description", "").strip()})
    return out


class NseArchives:
    """Primary EOD source. One request per file, at most one request every two seconds."""

    name = "nse_bhavcopy"

    def __init__(self, cache: DiskCache, *, fetchers: Optional[dict[str, Callable[[date], str]]] = None,
                 bucket: Optional[TokenBucket] = None):
        self.cache = cache
        self.bucket = bucket or TokenBucket(rate=0.5)
        if fetchers is None:
            from jugaad_data import nse

            fetchers = {"equity": nse.bhavcopy_raw, "full": nse.full_bhavcopy_raw, "index": nse.bhavcopy_index_raw}
        self._fetch = fetchers

    def _raw(self, kind: str, day: date) -> tuple[str, bool]:
        cached = self.cache.get(self.name, kind, {}, day, "csv")
        if cached is not None:
            return cached, True

        def go() -> str:
            self.bucket.acquire()
            try:
                text = self._fetch[kind](day)
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 404:
                    raise PermanentError(f"{kind} {day}: not published (holiday or not yet available)") from e
                raise
            if not text or len(text.splitlines()) < 2:
                raise RetryableError(f"{kind} {day}: empty file")
            return text

        text = with_retries(go)
        self.cache.put(self.name, kind, {}, day, text, "csv")
        return text, False

    def equity(self, day: date) -> FetchResult:
        text, hit = self._raw("equity", day)
        rows = parse_equity_bhavcopy(text)
        warnings = []
        try:
            full_text, _ = self._raw("full", day)
            deliv = parse_full_bhavcopy(full_text)
            for r in rows:
                r["delivery_pct"] = deliv.get(r["symbol"])
        except (PermanentError, RetryableError) as e:
            warnings.append(f"delivery data unavailable: {e}")
        return FetchResult(rows, self.name, utcnow(), cache_hit=hit, warnings=warnings)

    def indices(self, day: date) -> FetchResult:
        text, hit = self._raw("index", day)
        return FetchResult(parse_index_bhavcopy(text), "nse_index_bhavcopy", utcnow(), cache_hit=hit)

    def healthcheck(self) -> Health:
        t0 = time.perf_counter()
        try:
            from jugaad_data.nse import NSELive

            NSELive().market_status()
            return Health(True, int((time.perf_counter() - t0) * 1000))
        except Exception as e:
            return Health(False, int((time.perf_counter() - t0) * 1000), f"{type(e).__name__}: {e}")


def fetch_holidays() -> list[dict[str, Any]]:
    from jugaad_data.nse import NSELive

    return parse_holidays(with_retries(lambda: NSELive().holiday_list()))
