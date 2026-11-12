"""NSE corporate actions (unofficial JSON endpoint; needs the NSE cookie warm-up that NSELive does).

The `subject` field is free text such as
  "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
  "Bonus 1:1", "Interim Dividend - Rs 7.50 Per Share & Special Dividend Rs 86.50 Per Share",
  "Rights 3:8 @ Premium Rs 14/-", "Buy Back", "Demerger".
`parse_subject` turns it into a typed action; anything unrecognised is kept as `other`.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Optional

from .base import DiskCache, FetchResult, TokenBucket, utcnow, with_retries

_SPLIT = re.compile(r"split.*?from\s+(?:rs|re)\.?\s*([\d.]+).*?to\s+(?:rs|re)\.?\s*([\d.]+)", re.I)
_BONUS = re.compile(r"^\s*bonus\s+(\d+)\s*:\s*(\d+)", re.I)
_RIGHTS = re.compile(r"^\s*rights\s+(\d+)\s*:\s*(\d+)(?:.*?premium\s+(?:rs|re)\.?\s*([\d.]+))?", re.I)
_DIV_AMT = re.compile(r"(?:rs|re)\.?\s*([\d.]+)\s*(?:/-)?\s*per\s+share", re.I)


def parse_subject(subject: str) -> list[dict[str, Any]]:
    s = subject.strip()
    low = s.lower()
    if m := _SPLIT.search(s):
        old_fv, new_fv = Decimal(m.group(1)), Decimal(m.group(2))
        return [{"action_type": "split", "ratio_num": old_fv, "ratio_den": new_fv}]
    if "scheme of arrangement" in low:
        return [{"action_type": "other", "details": {"subject": s}}]
    if m := _BONUS.search(s):
        return [{"action_type": "bonus", "ratio_num": Decimal(m.group(1)), "ratio_den": Decimal(m.group(2))}]
    if m := _RIGHTS.search(s):
        return [{"action_type": "rights", "ratio_num": Decimal(m.group(1)), "ratio_den": Decimal(m.group(2)),
                 "details": {"premium": m.group(3)}}]
    if "dividend" in low or low.startswith("intdividend"):
        amounts = [Decimal(a) for a in _DIV_AMT.findall(s)]
        if amounts:
            return [{"action_type": "dividend", "amount_per_share": sum(amounts, Decimal(0)),
                     "details": {"subject": s}}]
    if "buy back" in low or "buyback" in low:
        return [{"action_type": "buyback"}]
    if "demerger" in low:
        return [{"action_type": "demerger"}]
    if "amalgamation" in low or "merger" in low:
        return [{"action_type": "merger"}]
    if low.startswith("interest payment") or low.startswith("redemption"):
        return []   # debt instruments
    return [{"action_type": "other", "details": {"subject": s}}]


def parse_actions(payload: list[dict]) -> list[dict[str, Any]]:
    out = []
    for item in payload:
        if item.get("series") not in ("EQ", "BE", "BZ"):
            continue
        try:
            ex = datetime.strptime(item["exDate"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        for a in parse_subject(item.get("subject", "")):
            out.append({"symbol": item["symbol"], "isin": item.get("isin"), "ex_date": ex,
                        "ratio_num": None, "ratio_den": None, "amount_per_share": None, "details": None, **a,
                        "raw_subject": item.get("subject")})
    return out


def share_multiplier(action_type: str, ratio_num: Optional[Decimal], ratio_den: Optional[Decimal]) -> Decimal:
    """How many shares one old share becomes. Used for price adjustment and simulator positions."""
    if action_type == "split" and ratio_num and ratio_den:
        return ratio_num / ratio_den               # FV 10 -> 2 : 5x shares
    if action_type == "bonus" and ratio_num and ratio_den:
        return (ratio_num + ratio_den) / ratio_den  # 1:1 bonus : 2x shares
    return Decimal(1)


class NseCorporateActions:
    name = "nse_corp_actions"
    URL = "https://www.nseindia.com/api/corporates-corporateActions"

    def __init__(self, cache: DiskCache, *, getter: Optional[Callable[[dict], list]] = None,
                 bucket: Optional[TokenBucket] = None):
        self.cache = cache
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._get = getter or self._live_get

    def _live_get(self, params: dict) -> list:
        from jugaad_data.nse import NSELive

        live = NSELive()
        r = live.s.get(self.URL, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def fetch(self, start: date, end: date) -> FetchResult:
        import json

        params = {"index": "equities", "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")}
        cached = self.cache.get(self.name, "actions", params, end, "json")
        if cached is not None:
            return FetchResult(parse_actions(json.loads(cached)), self.name, utcnow(), cache_hit=True)

        def go():
            self.bucket.acquire()
            return self._get(params)

        payload = with_retries(go)
        # Only cache windows that are fully in the past; today's list can still change.
        if end < date.today():
            self.cache.put(self.name, "actions", params, end, json.dumps(payload), "json")
        return FetchResult(parse_actions(payload), self.name, utcnow())
