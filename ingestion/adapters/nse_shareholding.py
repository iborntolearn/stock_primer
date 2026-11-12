"""NSE shareholding patterns (SEBI LODR Reg. 31): the master list per symbol, plus the per-filing XBRL
for the category breakdown and promoter encumbrance.

Privacy (§11): the XBRL lists individual shareholders with names and PAN. Only category totals are parsed,
and only that summary is cached; the raw file is never written to disk.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from .base import DiskCache, PermanentError, TokenBucket, with_retries

MASTER = "https://www.nseindia.com/api/corporate-share-holdings-master"
PARSER_VERSION = 3                     # part of the summary cache key: a parser change re-parses
# our field -> SHP context, normalised (lower case, "_Context" and the trailing instant "I" removed), so the
# 2025+ names ("MutualFundsOrUTI_ContextI") and the older ones ("MutualFundsOrUtiI", even "GovermentsI") both match.
CATEGORIES = {
    "promoter_pct": ("shareholdingofpromoterandpromotergroup",), "public_pct": ("publicshareholding",),
    "fpi_pct": ("institutionsforeign",), "dii_pct": ("institutionsdomestic",), "mf_pct": ("mutualfundsoruti",),
    "insurance_pct": ("insurancecompanies",), "government_pct": ("governments", "goverments"),
    "non_promoter_non_public_pct": ("sharesheldbynonpromoternonpublicshareholders",),
    "retail_small_pct": ("residentindividualshareholdersholdingnominalsharecapitaluptorstwolakh",),
    "retail_large_pct": ("residentindividualshareholdersholdingnominalsharecapitalinexcessofrstwolakh",),
}
PROMOTER, TOTAL = "shareholdingofpromoterandpromotergroup", "shareholdingpattern"
ENCUMBERED = ("EncumberedSharesHeldAsPercentageOfTotalNumberOfShares", "PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares")


def _norm(ctx: str) -> str:
    if "_Context" in ctx:                            # 2025+: "MutualFundsOrUTI_ContextI"
        return ctx.split("_Context")[0].lower()
    return (ctx[:-1] if ctx.endswith("I") else ctx).lower()     # older: "MutualFundsOrUtiI"


def _dec(x: Optional[str]) -> Optional[Decimal]:
    try:
        return Decimal(x.strip()) if x not in (None, "") else None
    except InvalidOperation:
        return None


def _dt(s: Optional[str]) -> Optional[datetime]:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime((s or "").strip().title(), fmt)
        except ValueError:
            continue
    return None


def is_quarter_end(d: date) -> bool:
    return (d.month, d.day) in ((3, 31), (6, 30), (9, 30), (12, 31))


def parse_shp(xml: str) -> dict[str, Any]:
    """Category totals as percentages (0-100), shareholder count, promoter encumbrance/pledge (% of promoter shares).
    Filings give either fractions (total = 1) or percentages (total = 100); the total decides the scale."""
    vals: dict[tuple[str, str], Decimal] = {}
    for m in re.finditer(r'<[a-z\-]+:(\w+)\b[^>]*contextRef="([^"]+)"[^>]*>([^<]*)<', xml):
        v = _dec(m.group(3))
        if v is not None:
            vals.setdefault((m.group(1), _norm(m.group(2))), v)

    def get(tag: str, ctxs: tuple[str, ...]) -> Optional[Decimal]:
        return next((vals[(tag, c)] for c in ctxs if (tag, c) in vals), None)

    pct = "ShareholdingAsAPercentageOfTotalNumberOfShares"
    total = get(pct, (TOTAL,))
    promoter = get(pct, (PROMOTER,))
    scale = Decimal(100) if (total is not None and total <= Decimal("1.5")) or (total is None and promoter is not None and promoter <= 1) else Decimal(1)
    q = Decimal("0.0001")
    out: dict[str, Any] = {}
    for k, ctxs in CATEGORIES.items():
        v = get(pct, ctxs)
        out[k] = None if v is None else (v * scale).quantize(q)
    if out["non_promoter_non_public_pct"] is None:      # some filers only fill the sub-categories (ESOP trusts, DR custodian)
        parts = [get(pct, (c,)) for c in ("employeebenefitstrusts", "custodianordrholder")]
        if any(v for v in parts if v):
            out["non_promoter_non_public_pct"] = (sum(v for v in parts if v) * scale).quantize(q)
    out["no_promoter_group"] = out["promoter_pct"] is None and out["public_pct"] is not None
    if out["no_promoter_group"]:                       # professionally managed (e.g. HDFC Bank, ITC, L&T): 0%, not missing
        out["promoter_pct"] = Decimal(0)
    rs, rl = out.pop("retail_small_pct"), out.pop("retail_large_pct")
    out["retail_pct"] = None if rs is None and rl is None else (rs or 0) + (rl or 0)
    n = get("NumberOfShareholders", (TOTAL,))
    out["shareholders"] = int(n) if n is not None else None
    enc = next((v for v in (get(t, (PROMOTER,)) for t in ENCUMBERED) if v is not None), None)
    out["promoter_encumbered_pct"] = Decimal(0) if enc is None else (enc * scale).quantize(q)
    pledged = next((v for (tag, c), v in vals.items() if c == PROMOTER and "Pledge" in tag and "Percentage" in tag
                    and not tag.startswith("PledgedOrEncumbered")), None)
    flag = re.search(r'WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged\b[^>]*>([^<]*)<', xml)
    if pledged is not None:
        out["promoter_pledged_pct"] = (pledged * scale).quantize(q)
    elif flag and flag.group(1).strip().lower() == "false":
        out["promoter_pledged_pct"] = Decimal(0)
    else:
        out["promoter_pledged_pct"] = None          # older format: only the combined pledged-or-encumbered figure
    rep = re.search(r'<[a-z\-]+:DateOfReport\b[^>]*>([^<]*)<', xml)
    out["date_of_report"] = rep.group(1).strip() if rep else None
    return out


@dataclass
class ShpFiling:
    symbol: str
    isin: str
    period_end: date
    published_at: datetime          # broadcast (a revision's later broadcast), IST wall-clock
    xbrl: Optional[str]
    revised: bool
    promoter_pct: Optional[Decimal]
    public_pct: Optional[Decimal]


def parse_master(payload: Any) -> list[ShpFiling]:
    rows = payload.get("data", payload) if isinstance(payload, dict) else (payload or [])
    out = []
    for r in rows:
        pe, pub = _dt(r.get("date")), _dt(r.get("broadcastDate") or r.get("submissionDate"))
        if pe is None or pub is None or not is_quarter_end(pe.date()):
            continue                                 # event-driven filings (e.g. before an allotment) are skipped
        xbrl = r.get("xbrl")
        out.append(ShpFiling(r.get("symbol", ""), r.get("isin", ""), pe.date(), pub,
                             None if not xbrl or xbrl.endswith("/null") else xbrl,
                             (r.get("revisedData") or "N") not in ("N", "", None),
                             _dec(r.get("pr_and_prgrp")), _dec(r.get("public_val"))))
    return out


class NseShareholding:
    name = "nse_shareholding"

    def __init__(self, cache: DiskCache, *, get_json: Optional[Callable[[str, dict], Any]] = None,
                 get_text: Optional[Callable[[str], str]] = None, bucket: Optional[TokenBucket] = None):
        self.cache = cache
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._get_json = get_json or self._live_json
        self._get_text = get_text or self._live_text
        self._live = self._arch = None

    def _live_json(self, url: str, params: dict) -> Any:
        if self._live is None:
            from jugaad_data.nse import NSELive

            self._live = NSELive()
        r = self._live.s.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def _live_text(self, url: str) -> str:
        if self._arch is None:
            from jugaad_data.nse.archives import NSEArchives

            self._arch = NSEArchives()
        r = self._arch.s.get(url, timeout=60)
        if r.status_code == 404:
            raise PermanentError(f"{url}: not found")
        r.raise_for_status()
        return r.text

    def filings(self, symbol: str) -> list[ShpFiling]:
        def go():
            self.bucket.acquire()
            return self._get_json(MASTER, {"index": "equities", "symbol": symbol})
        return parse_master(with_retries(go, attempts=2))

    def summary(self, f: ShpFiling) -> dict[str, Any]:
        """Parsed category totals for one filing (cached as the summary only)."""
        if not f.xbrl:
            raise PermanentError(f"{f.symbol} {f.period_end}: no XBRL")
        key = {"url": f.xbrl, "parser": PARSER_VERSION}
        cached = self.cache.get(self.name, "summary", key, f.period_end, "json")
        if cached is not None:
            return json.loads(cached, parse_float=Decimal)

        def go():
            self.bucket.acquire()
            return self._get_text(f.xbrl)
        out = parse_shp(with_retries(go, attempts=2))
        self.cache.put(self.name, "summary", key, f.period_end,
                       json.dumps({k: float(v) if isinstance(v, Decimal) else v for k, v in out.items()}), "json")
        return out
