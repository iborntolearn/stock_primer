"""Google News RSS (§4.3 stock news fallback 1). Free, no key, query by company name.

Items are external free text: they are stored and passed to agents as untrusted
data only. Titles end with " - <Publisher>", which is split off as the source.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Callable, Optional
from urllib.parse import quote_plus

from .base import FetchResult, TokenBucket, utcnow, with_retries

FEED = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
_TAG = re.compile(r"<[^>]+>")


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        if not link or not title or not pub:
            continue
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        snippet = _TAG.sub(" ", unescape(item.findtext("description") or "")).strip()
        out.append({"url": link, "url_hash": url_hash(link), "title": title, "source": source or "google_news",
                    "published_at": published, "snippet": re.sub(r"\s+", " ", snippet)[:400]})
    return out


class GoogleNews:
    name = "google_news_rss"

    def __init__(self, getter: Optional[Callable[[str], str]] = None, bucket: Optional[TokenBucket] = None):
        self.bucket = bucket or TokenBucket(rate=0.3)
        self._get = getter or self._live_get

    @staticmethod
    def _live_get(url: str) -> str:
        import requests

        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.text

    def search(self, query: str) -> FetchResult:
        url = FEED.format(q=quote_plus(query))

        def go():
            self.bucket.acquire()
            return self._get(url)

        return FetchResult(parse_feed(with_retries(go, attempts=2)), self.name, utcnow())
