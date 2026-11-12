"""BSE as a second source, for companies whose filings are not on NSE (e.g. MCX, which files only with BSE).

- results PDFs from BSE corporate announcements (category "Result" / "Financial Results"), in the same
  ResultPdf shape as NSE so the PDF -> OCR/images -> LLM-15 -> checks pipeline is reused unchanged;
- the shareholding summary (promoter / public / non-promoter-non-public) from BSE's summary statement, and
  the public-shareholder categories (FPI, DII, MF, insurance, government, retail) from the table behind
  BSE's "public shareholder" page. BSE gives no filing timestamp, so the publication date is taken as the
  SEBI deadline (21 days after the quarter), noted in `raw`. The category table also names large holders;
  only category totals are parsed and only those are cached.

BSE's API wants browser-like requests (Referer/Origin); curl_cffi with the corporate CA bundle does that.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from .base import DiskCache, PermanentError, TokenBucket, with_retries
from .nse_announcements import ResultPdf, results_due

API = "https://api.bseindia.com/BseIndiaAPI/api"
ATTACH = ("https://www.bseindia.com/xml-data/corpfiling/AttachLive/", "https://www.bseindia.com/xml-data/corpfiling/AttachHis/")
HEADERS = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com", "Accept": "application/json, text/plain, */*"}
MAX_PDF_BYTES = 25 * 1024 * 1024
SHP_DEADLINE_DAYS = 21


def qtrid(period_end: date) -> int:
    """BSE shareholding quarter id: sequential, 130 = June 2026 (checked against BSE for 2024-2026)."""
    return period_end.year * 4 + period_end.month // 3 - 7976


def parse_search(text: str) -> list[dict[str, str]]:
    """PeerSmartSearch returns HTML list items: name, symbol, ISIN, scrip code."""
    out = []
    for m in re.finditer(r"liclick\('(\d+)','([^']*)'\).*?<strong>([^<]*)</strong>\s*(?:&nbsp;)*\s*([A-Z0-9]{12})", text, re.S):
        out.append({"code": m.group(1), "name": html.unescape(m.group(2)), "symbol": m.group(3).strip(), "isin": m.group(4)})
    return out


def parse_results(payload: Any, symbol: str, period_end: date) -> list[ResultPdf]:
    rows = payload.get("Table", []) if isinstance(payload, dict) else []
    out = []
    for r in rows:
        name, sub = r.get("ATTACHMENTNAME") or "", (r.get("SUBCATNAME") or "").lower()
        if not name.lower().endswith(".pdf") or "financial result" not in sub:
            continue
        try:
            when = datetime.fromisoformat((r.get("NEWS_DT") or r.get("DT_TM") or "")[:19])
        except ValueError:
            continue
        out.append(ResultPdf(symbol, period_end, ATTACH[0] + name, when, r.get("NEWSSUB") or "Financial Results",
                             r.get("HEADLINE") or "", 3))
    return sorted(out, key=lambda x: x.published_at)


def _num(tok: str) -> Optional[Decimal]:
    try:
        return Decimal(tok.replace(",", ""))
    except InvalidOperation:
        return None


SHP_LABELS = {"promoter_pct": "(A) Promoter & Promoter Group", "public_pct": "(B) Public",
              "dr_pct": "(C1) Shares underlying DRs", "trust_pct": "(C2) Shares held by Employee Trust", "total_pct": "Grand Total"}


def parse_shp_summary(data_html: str) -> dict[str, Any]:
    """BSE summary statement -> percentages (0-100). Each category label is followed by: holders, fully paid
    shares, total shares, % of total ...; a category with no holders shows zeros and '--'."""
    text = re.sub(r"<[^>]+>", " | ", data_html)
    tokens = [t.strip() for t in html.unescape(text).split("|") if t.strip()]
    q = re.search(r"Quarter ending\s*:\s*([A-Za-z]+ \d{4})", " ".join(tokens))
    out: dict[str, Any] = {"quarter": q.group(1) if q else None}
    labels = set(SHP_LABELS.values()) | {"(C) Non Promoter-Non Public", "Notes"}
    for key, label in SHP_LABELS.items():
        try:
            i = tokens.index(label)
        except ValueError:
            out[key] = None
            continue
        nums = []
        for t in tokens[i + 1:]:
            if t in labels or t.startswith("("):
                break
            nums.append(_num(t))
        vals = [n for n in nums[:4]]
        if len(vals) >= 4 and vals[3] is not None:
            out[key] = vals[3]
        elif vals and all(v == 0 for v in vals if v is not None):
            out[key] = Decimal(0)                          # e.g. no promoter group: zeros and '--'
        else:
            out[key] = None
        if key == "total_pct" and len(nums) >= 1 and nums[0] is not None:
            out["shareholders"] = int(nums[0])
    rest = sum((out.get(k) or 0) for k in ("public_pct", "trust_pct", "dr_pct"))
    out["no_promoter_group"] = out.get("promoter_pct") in (None, 0) and out.get("public_pct") is not None and abs(rest - 100) <= 1
    if out["promoter_pct"] is None and out["no_promoter_group"]:
        out["promoter_pct"] = Decimal(0)                   # no promoter row at all: professionally managed
    flag = re.search(r"shares held by promoters are pledge or otherwise encumbered\?\s*\|\s*(Yes|No)", " | ".join(tokens), re.I)
    out["promoter_encumbered_pct"] = Decimal(0) if flag and flag.group(1).lower() == "no" else None
    return out


def _norm(x: Optional[str]) -> str:
    return " ".join((x or "").lower().split())


CATEGORY_PARSER_VERSION = 1


def parse_public_categories(payload: Any) -> dict[str, Any]:
    """Category totals (rows without a holder name) from Corp_shpSec_SHPPubShold_ng. Subtotals are matched on
    label *and* parent category, so an unfamiliar layout yields None rather than a wrong number."""
    rows = [r for r in (payload or {}).get("Table1", []) if not r.get("Fld_ShareHolderName")]

    def pct(level_test, sub_test=lambda sc: True) -> Optional[Decimal]:
        for r in rows:
            if level_test(_norm(r.get("Fld_Level"))) and sub_test(_norm(r.get("Fld_SubCategory"))):
                v = r.get("Fld_TotalPercentageOf_A_B_C2")
                return None if v is None else Decimal(str(v))
        return None

    small = pct(lambda lv: lv.startswith("resident individuals holding nominal share capital up to"))
    large = pct(lambda lv: lv.startswith("resident individuals holding nominal share capital in excess"))
    return {
        "dii_pct": pct(lambda lv: lv == "sub total b1", lambda sc: sc == "institutions (domestic)"),
        "fpi_pct": pct(lambda lv: lv == "sub total b2", lambda sc: sc == "institutions (foreign)"),
        "mf_pct": pct(lambda lv: lv.startswith("mutual funds")),
        "insurance_pct": pct(lambda lv: lv == "insurance companies"),
        "government_pct": pct(lambda lv: lv.startswith("sub total"), lambda sc: sc.startswith("central government")),
        "retail_pct": None if small is None and large is None else (small or 0) + (large or 0),
        "public_total_pct": pct(lambda lv: lv.replace(" ", "") == "b=b1+b2+b3+b4"),
    }


@dataclass
class BseShp:
    symbol: str
    period_end: date
    published_at: datetime        # estimated: SEBI deadline (BSE's summary has no timestamp)
    summary: dict[str, Any]


class Bse:
    name = "bse"

    def __init__(self, cache: DiskCache, *, get: Optional[Callable[[str, dict], Any]] = None,
                 get_bytes: Optional[Callable[[str], bytes]] = None, bucket: Optional[TokenBucket] = None,
                 today: Callable[[], date] = date.today):
        self.cache = cache
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._get = get or self._live_get
        self._get_bytes = get_bytes or self._live_bytes
        self.today = today
        self._session = None
        self._codes: dict[str, str] = {}

    def _s(self):
        if self._session is None:
            from curl_cffi import requests as creq

            from core.net import ca_bundle_path

            self._session = creq.Session(impersonate="chrome", verify=ca_bundle_path())
        return self._session

    def _live_get(self, path: str, params: dict) -> Any:
        r = self._s().get(f"{API}/{path}", params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("application/json"):
            raise PermanentError(f"BSE {path}: not JSON (endpoint changed?)")
        return r.json()

    def _live_bytes(self, url: str) -> bytes:
        r = self._s().get(url, headers={"Referer": "https://www.bseindia.com/"}, timeout=60)
        if r.status_code == 404:
            raise PermanentError(f"{url}: not found")
        r.raise_for_status()
        return r.content

    def _call(self, path: str, params: dict) -> Any:
        def go():
            self.bucket.acquire()
            return self._get(path, params)
        return with_retries(go, attempts=2)

    def scrip_code(self, symbol: str, isin: Optional[str] = None) -> Optional[str]:
        """BSE scrip code, matched on ISIN when known (symbols can differ between exchanges)."""
        key = (symbol.upper(), isin)
        if key in self._codes:
            return self._codes[key]
        cached = self.cache.get(self.name, "scrip", {"symbol": symbol.upper(), "isin": isin}, date(2000, 1, 1), "json")
        if cached is not None:
            code = json.loads(cached)
        else:
            raw = self._call("PeerSmartSearch/w", {"Type": "SS", "text": symbol})
            hits = parse_search(raw if isinstance(raw, str) else json.dumps(raw))
            pick = next((h for h in hits if isin and h["isin"] == isin), None) or \
                next((h for h in hits if h["symbol"].upper() == symbol.upper()), None)
            code = pick["code"] if pick else None
            self.cache.put(self.name, "scrip", {"symbol": symbol.upper(), "isin": isin}, date(2000, 1, 1), json.dumps(code), "json")
        self._codes[key] = code
        return code

    # -- results PDFs (same interface as NseAnnouncements) ----------------------------------------------
    def result_pdfs(self, symbol: str, period_end: date, isin: Optional[str] = None) -> list[ResultPdf]:
        code = self.scrip_code(symbol, isin)
        if not code:
            return []
        start, end = period_end + timedelta(days=1), min(results_due(period_end) + timedelta(days=20), self.today())
        params = dict(pageno=1, strCat="Result", strPrevDate=start.strftime("%Y%m%d"), strScrip=code, strSearch="P",
                      strToDate=end.strftime("%Y%m%d"), strType="C", subcategory=-1)
        settled = end < self.today() - timedelta(days=2)
        cached = self.cache.get(self.name, "results", params, end, "json") if settled else None
        payload = json.loads(cached) if cached is not None else self._call("AnnSubCategoryGetData/w", params)
        if cached is None and settled:
            self.cache.put(self.name, "results", params, end, json.dumps(payload), "json")
        return parse_results(payload, symbol, period_end)

    def pdf(self, doc: ResultPdf) -> bytes:
        key = {"url": doc.url}
        data = self.cache.get_bytes(self.name, "pdf", key, doc.period_end, "pdf")
        if data is not None:
            return data
        last: Optional[Exception] = None
        for base in ATTACH:                               # older filings move from AttachLive to AttachHis
            url = base + doc.url.rsplit("/", 1)[-1]
            try:
                def go(url=url):
                    self.bucket.acquire()
                    return self._get_bytes(url)
                data = with_retries(go, attempts=2)
                break
            except PermanentError as e:
                last = e
        else:
            raise last or PermanentError(f"{doc.url}: not found")
        if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF"):
            raise PermanentError(f"{doc.url}: not a usable PDF")
        self.cache.put_bytes(self.name, "pdf", key, doc.period_end, data, "pdf")
        return data

    def public_categories(self, code: str, period_end: date) -> Optional[dict[str, Any]]:
        """Parsed category totals for one quarter (cached as totals only; the raw table names large holders)."""
        params = {"SCRIPCODE": code, "QtrCode": str(qtrid(period_end))}
        key = {**params, "parser": CATEGORY_PARSER_VERSION}
        cached = self.cache.get(self.name, "shp_categories", key, period_end, "json")
        if cached is not None:
            return json.loads(cached, parse_float=Decimal)
        try:
            payload = self._call("Corp_shpSec_SHPPubShold_ng/w", params)
        except Exception:
            return None
        cats = parse_public_categories(payload)
        if cats.get("public_total_pct") is None:
            return None                                   # empty or unfamiliar table
        self.cache.put(self.name, "shp_categories", key, period_end,
                       json.dumps({k: float(v) if isinstance(v, Decimal) else v for k, v in cats.items()}), "json")
        return cats

    # -- shareholding summary ------------------------------------------------------------------------------
    def shareholding(self, symbol: str, period_end: date, isin: Optional[str] = None) -> Optional[BseShp]:
        code = self.scrip_code(symbol, isin)
        if not code:
            return None
        params = {"scripcode": code, "qtrid": str(qtrid(period_end))}
        # The summary statement is aggregates only (no names/PANs), so the raw HTML is cached and parsed on
        # every read: parser fixes apply to cached quarters too.
        data = self.cache.get(self.name, "shp_html", params, period_end, "html")
        if data is None:
            payload = self._call("shpSecSummery_New/w", params)
            data = (payload or {}).get("Data") if isinstance(payload, dict) else None
            if not data:
                return None
            self.cache.put(self.name, "shp_html", params, period_end, data, "html")
        summary = parse_shp_summary(data)
        if summary.get("quarter") != period_end.strftime("%B %Y"):
            return None                                        # BSE answered with a different quarter
        # End of the deadline day: the latest it can have been public, so nothing is used before it was.
        cats = self.public_categories(code, period_end)
        if cats:
            summary = {**summary, **{k: v for k, v in cats.items() if k != "public_total_pct"},
                       "categories_total_pct": cats.get("public_total_pct")}
        return BseShp(symbol, period_end, datetime.combine(period_end + timedelta(days=SHP_DEADLINE_DAYS), time(23, 59)), summary)
