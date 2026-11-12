"""World news and official releases (§4.3 world events).

Sources:
- GDELT DOC API (doc primary). On this network it answers 429 even to single, spaced
  requests (shared IP), so it is optional and failures degrade quietly.
- Google News RSS topic searches over the same themes (fallback that works here).
- Official RSS: RBI press releases and SEBI. PIB's English feed URL could not be
  found (the known one serves Hindi without dates), so PIB is left out for now.
All text is untrusted external content. Items naming private individuals with PAN
numbers (SEBI recovery notices) are dropped, and PAN-shaped strings are redacted (§11).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Callable, Optional

from .base import FetchResult, TokenBucket, utcnow, with_retries
from .gnews import GoogleNews, url_hash

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
THEMES = {
    "india_macro": '(India OR Indian) (economy OR inflation OR "interest rate" OR RBI OR rupee)',
    "oil": '("crude oil" OR Brent OR OPEC OR "oil prices")',
    "us_rates_trade": '("Federal Reserve" OR "Treasury yields" OR "US inflation" OR tariffs)',
    "geopolitics": '(war OR sanctions OR conflict OR ceasefire OR "trade deal") (India OR China OR "Middle East" OR Russia)',
}
RSS_FEEDS = {"RBI": "https://www.rbi.org.in/pressreleases_rss.xml", "SEBI": "https://www.sebi.gov.in/sebirss.xml"}
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
PERSONAL = re.compile(r"recovery certificate|notice of demand|notice of attachment|attachment of|issued against|"
                      r"\bappeal no\.|filed by|in respect of (mr|ms|mrs|shri|smt)\b", re.I)
_TAG = re.compile(r"<[^>]+>")
_DATE_FORMATS = ("%d %b %Y %H:%M:%S %z", "%d %b, %Y %H:%M:%S %z", "%d %b, %Y %z", "%d %b %Y %z", "%a, %d %b %Y %H:%M:%S %Z")


def _date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
            return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _mostly_english(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) > 0.8


def clean(item: dict) -> Optional[dict]:
    """Privacy filter (§11): drop items about named individuals; redact PAN-shaped strings."""
    if PERSONAL.search(item["title"]) or PAN.search(item["title"]):
        return None
    item["snippet"] = PAN.sub("[redacted]", item.get("snippet") or "")
    return item


def parse_gdelt(payload: dict) -> list[dict[str, Any]]:
    out = []
    for a in (payload or {}).get("articles", []):
        url, title = a.get("url"), (a.get("title") or "").strip()
        try:
            published = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if url and title:
            out.append({"url": url, "url_hash": url_hash(url), "title": title[:300], "published_at": published,
                        "source": a.get("domain") or "gdelt", "snippet": f"{a.get('sourcecountry', '')}".strip()})
    return out


def parse_rss(content: bytes | str, source: str) -> list[dict[str, Any]]:
    data = content.encode("utf-8") if isinstance(content, str) else content
    root = ET.fromstring(data.lstrip(b"\xef\xbb\xbf").strip())
    out = []
    for item in root.iter("item"):
        title = unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        published = _date(item.findtext("pubDate"))
        if not title or not link or published is None or not _mostly_english(title):
            continue
        desc = re.sub(r"\s+", " ", _TAG.sub(" ", unescape(item.findtext("description") or ""))).strip()
        cleaned = clean({"url": link, "url_hash": url_hash(link), "title": title[:300], "published_at": published,
                         "source": source, "snippet": desc[:400]})
        if cleaned:
            out.append(cleaned)
    return out


class WorldNews:
    name = "world_news"

    def __init__(self, get_json: Optional[Callable[[str, dict], dict]] = None,
                 get_bytes: Optional[Callable[[str], bytes]] = None, gnews: Optional[GoogleNews] = None,
                 use_gdelt: bool = True, gdelt_bucket: Optional[TokenBucket] = None):
        self._get_json = get_json or self._live_json
        self._get_bytes = get_bytes or self._live_bytes
        self.gnews = gnews or GoogleNews()
        self.use_gdelt = use_gdelt
        self.gdelt_bucket = gdelt_bucket or TokenBucket(rate=0.18)     # GDELT asks for < 1 request / 5 s
        self.rss_bucket = TokenBucket(rate=1.0)

    @staticmethod
    def _live_json(url: str, params: dict) -> dict:
        import requests

        r = requests.get(url, params=params, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _live_bytes(url: str) -> bytes:
        import requests

        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content

    def themes(self, timespan: str = "1d", per_query: int = 50) -> FetchResult:
        """World news for the event pipeline: GDELT when it answers, Google News topic search otherwise."""
        items, warnings = [], []
        for theme, q in THEMES.items():
            got = []
            if self.use_gdelt:
                def go(q=q):
                    self.gdelt_bucket.acquire()
                    return self._get_json(GDELT, {"query": f"{q} sourcelang:english", "mode": "artlist", "format": "json",
                                                  "maxrecords": str(per_query), "timespan": timespan, "sort": "datedesc"})
                try:
                    got = parse_gdelt(with_retries(go, attempts=1))
                except Exception as e:
                    warnings.append(f"gdelt {theme}: {type(e).__name__}")
            if not got:
                try:
                    got = self.gnews.search(q.replace("(", "").replace(")", "") + " when:1d").records
                except Exception as e:
                    warnings.append(f"google news {theme}: {type(e).__name__}")
            items += [x for x in (clean(dict(i)) for i in got) if x]
        return FetchResult(items, "world_news", utcnow(), warnings=warnings)

    def official(self) -> FetchResult:
        items, warnings = [], []
        for src, url in RSS_FEEDS.items():
            def go(url=url):
                self.rss_bucket.acquire()
                return self._get_bytes(url)
            try:
                items += parse_rss(with_retries(go, attempts=2), src)
            except Exception as e:
                warnings.append(f"{src}: {type(e).__name__}: {str(e)[:120]}")
        return FetchResult(items, "official_rss", utcnow(), warnings=warnings)
