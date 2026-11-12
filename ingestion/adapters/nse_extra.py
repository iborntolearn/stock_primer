"""More NSE endpoints (unofficial, cookie warm-up via NSELive):
- FII/DII provisional cash-market flows (latest day only; history accumulates as we ingest daily)
- board meetings: upcoming results dates, the key catalyst for the event calendar
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from .base import FetchResult, TokenBucket, utcnow, with_retries

FLOWS_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
BOARD_URL = "https://www.nseindia.com/api/corporate-board-meetings"


def _dec(x: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(x).replace(",", ""))
    except (InvalidOperation, TypeError):
        return None


def parse_flows(payload: list[dict]) -> list[dict[str, Any]]:
    """[{category: DII|FII/FPI, date: 13-Oct-2026, netValue}] -> [{trade_date, fii_net, dii_net}] in ₹ crore."""
    by_date: dict = {}
    for r in payload or []:
        try:
            d = datetime.strptime(r["date"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        row = by_date.setdefault(d, {"trade_date": d, "fii_net": None, "dii_net": None})
        cat = (r.get("category") or "").upper()
        if cat.startswith("FII") or "FPI" in cat:
            row["fii_net"] = _dec(r.get("netValue"))
        elif cat.startswith("DII"):
            row["dii_net"] = _dec(r.get("netValue"))
    return list(by_date.values())


def parse_board_meetings(payload: list[dict]) -> list[dict[str, Any]]:
    """Board meetings whose purpose includes financial results -> results-date catalysts."""
    out = []
    for r in payload or []:
        purpose = (r.get("bm_purpose") or "")
        try:
            d = datetime.strptime(r["bm_date"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        out.append({"symbol": r.get("bm_symbol"), "date": d, "purpose": purpose.strip(),
                    "is_results": "result" in purpose.lower(), "desc": (r.get("bm_desc") or "")[:300]})
    return [x for x in out if x["symbol"]]


class NseExtra:
    name = "nse_extra"

    def __init__(self, getter: Optional[Callable[[str, dict], Any]] = None, bucket: Optional[TokenBucket] = None):
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._get = getter or self._live
        self._live_client = None

    def _live(self, url: str, params: dict) -> Any:
        if self._live_client is None:
            from jugaad_data.nse import NSELive

            self._live_client = NSELive()
        r = self._live_client.s.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def _call(self, url: str, params: dict) -> Any:
        def go():
            self.bucket.acquire()
            return self._get(url, params)
        return with_retries(go, attempts=2)

    def flows(self) -> FetchResult:
        return FetchResult(parse_flows(self._call(FLOWS_URL, {})), "nse_fiidii", utcnow())

    def board_meetings(self, start=None, end=None) -> FetchResult:
        params = {"index": "equities"}
        if start and end:
            params |= {"from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")}
        return FetchResult(parse_board_meetings(self._call(BOARD_URL, params)), "nse_board_meetings", utcnow())
