"""Nifty index constituents from niftyindices.com (Company Name, Industry, Symbol, Series, ISIN Code).

The free list is a current snapshot only. Membership history is built forward
from the first snapshot (valid_from = first day seen); see ingestion.universe.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any, Callable, Optional

from .base import DiskCache, FetchResult, utcnow, with_retries

LISTS = {
    "NIFTY50": "ind_nifty50list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY200": "ind_nifty200list.csv",
    "NIFTY500": "ind_nifty500list.csv",
}
BASE = "https://www.niftyindices.com/IndexConstituent/"


def parse_constituents(text: str) -> list[dict[str, Any]]:
    rows = csv.DictReader(io.StringIO(text.strip()))
    return [{"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(), "industry": r["Industry"].strip(),
             "series": r["Series"].strip(), "isin": r["ISIN Code"].strip()} for r in rows if r.get("Symbol")]


class NiftyConstituents:
    name = "niftyindices"

    def __init__(self, cache: DiskCache, getter: Optional[Callable[[str], str]] = None):
        self.cache = cache
        self._get = getter or self._live_get

    @staticmethod
    def _live_get(url: str) -> str:
        import requests

        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.text

    def fetch(self, universe: str, as_of: date) -> FetchResult:
        url = BASE + LISTS[universe]
        text = self.cache.get(self.name, universe, {}, as_of, "csv")
        hit = text is not None
        if not hit:
            text = with_retries(lambda: self._get(url))
            self.cache.put(self.name, universe, {}, as_of, text, "csv")
        return FetchResult(parse_constituents(text), self.name, utcnow(), cache_hit=hit)
