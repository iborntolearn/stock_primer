"""NSE corporate announcements: find and download a quarter's results PDF (fallback when no XBRL is listed).

Results PDFs are attached to announcements such as "Financial Result Updates", "Integrated Filing
(Financial)" or "Outcome of Board Meeting" (the board outcome usually carries the signed results).
Announcement time is the publication time, which keeps the data point-in-time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from .base import DiskCache, PermanentError, TokenBucket, with_retries

ANNOUNCEMENTS = "https://www.nseindia.com/api/corporate-announcements"
MAX_PDF_BYTES = 25 * 1024 * 1024
SKIP_WORDS = ("transcript", "recording", "record date", "investor meet", "con. call", "press release", "newspaper",
              "trading window", "intimation of board meeting", "prior intimation")


def results_due(period_end: date) -> date:
    """SEBI LODR: 45 days after a quarter, 60 for the year-end (March) quarter."""
    return period_end + timedelta(days=60 if period_end.month == 3 else 45)


def _dt(s: Optional[str]) -> Optional[datetime]:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime((s or "").strip(), fmt)
        except ValueError:
            continue
    return None


@dataclass
class ResultPdf:
    symbol: str
    period_end: date
    url: str
    published_at: datetime          # IST wall-clock, as NSE shows it
    desc: str
    text: str
    score: int


def parse_announcements(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("data", payload) if isinstance(payload, dict) else (payload or [])
    out = []
    for r in rows:
        when = _dt(r.get("an_dt") or r.get("sort_date"))
        url = r.get("attchmntFile") or ""
        if when is None or not url or url.endswith("/null"):
            continue
        out.append({"symbol": r.get("symbol"), "when": when, "desc": (r.get("desc") or "").strip(),
                    "text": (r.get("attchmntText") or "").strip(), "url": url})
    return out


def pick_result_pdfs(rows: list[dict[str, Any]], symbol: str, period_end: date) -> list[ResultPdf]:
    """Candidate results PDFs for the quarter, best first (results-specific descriptions, then earliest)."""
    start, end = period_end + timedelta(days=1), results_due(period_end) + timedelta(days=20)
    out = []
    for r in rows:
        if not (start <= r["when"].date() <= end) or not r["url"].lower().endswith(".pdf"):
            continue
        desc, text = r["desc"].lower(), r["text"].lower()
        if any(w in desc or w in text for w in SKIP_WORDS):
            continue
        if "financial result" in desc or "integrated filing (financial)" in desc:
            score = 3
        elif "outcome of board meeting" in desc:
            score = 2 if any(w in text for w in ("result", "financial")) else 1
        elif "result" in desc or ("financial results" in text and "quarter" in text):
            score = 1
        else:
            continue
        out.append(ResultPdf(symbol, period_end, r["url"], r["when"], r["desc"], r["text"], score))
    return sorted(out, key=lambda x: (-x.score, x.published_at))


class NseAnnouncements:
    name = "nse_announcements"

    def __init__(self, cache: DiskCache, *, get_json: Optional[Callable[[str, dict], Any]] = None,
                 get_bytes: Optional[Callable[[str], bytes]] = None, bucket: Optional[TokenBucket] = None,
                 today: Callable[[], date] = date.today):
        self.cache = cache
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._get_json = get_json or self._live_json
        self._get_bytes = get_bytes or self._live_bytes
        self.today = today
        self._live = self._arch = None

    def _live_json(self, url: str, params: dict) -> Any:
        if self._live is None:
            from jugaad_data.nse import NSELive

            self._live = NSELive()
        r = self._live.s.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def _live_bytes(self, url: str) -> bytes:
        if self._arch is None:
            from jugaad_data.nse.archives import NSEArchives

            self._arch = NSEArchives()
        r = self._arch.s.get(url, timeout=60)
        if r.status_code == 404:
            raise PermanentError(f"{url}: not found")
        r.raise_for_status()
        return r.content

    def result_pdfs(self, symbol: str, period_end: date) -> list[ResultPdf]:
        start, end = period_end + timedelta(days=1), min(results_due(period_end) + timedelta(days=20), self.today())
        params = {"index": "equities", "symbol": symbol, "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")}
        settled = end < self.today() - timedelta(days=2)          # a closed window never changes, so it can be cached
        cached = self.cache.get(self.name, "list", params, end, "json") if settled else None
        if cached is None:
            def go():
                self.bucket.acquire()
                return self._get_json(ANNOUNCEMENTS, params)
            payload = with_retries(go, attempts=2)
            if settled:
                self.cache.put(self.name, "list", params, end, json.dumps(payload), "json")
        else:
            payload = json.loads(cached)
        return pick_result_pdfs(parse_announcements(payload), symbol, period_end)

    def pdf(self, doc: ResultPdf) -> bytes:
        key = {"url": doc.url}
        data = self.cache.get_bytes(self.name, "pdf", key, doc.period_end, "pdf")
        if data is None:
            def go():
                self.bucket.acquire()
                return self._get_bytes(doc.url)
            data = with_retries(go, attempts=2)
            if len(data) > MAX_PDF_BYTES:
                raise PermanentError(f"{doc.url}: {len(data) / 1e6:.0f} MB is over the {MAX_PDF_BYTES // 2**20} MB limit")
            if not data.startswith(b"%PDF"):
                raise PermanentError(f"{doc.url}: not a PDF")
            self.cache.put_bytes(self.name, "pdf", key, doc.period_end, data, "pdf")
        return data
