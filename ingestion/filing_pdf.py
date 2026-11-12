"""Results PDFs -> fundamental_snapshot, for quarters with no XBRL listed (LLM-15 fallback, phase 4).

    find_gaps()      quarters missing for listed companies whose results are past the SEBI due date
    fill_gap()       announcement PDF -> text (pypdf) -> the statement page(s) -> LLM-15 -> deterministic checks
                     -> stored, or a data_anomaly review task

Only the one or two pages holding the quarterly statement go to the model (cheap and focused). Scanned
pages (image-only, or with an unreadable text layer) go through OCR to find the statement; the model then
reads the values from the page images, with the OCR transcript as a guide (see ingestion/ocr.py).
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m

from .adapters.nse_announcements import NseAnnouncements, results_due

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
MAX_CHARS = 16000


def pdf_pages(data: bytes) -> list[str]:
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        return [(p.extract_text() or "") for p in reader.pages]
    except Exception as e:                       # malformed PDFs raise many types
        log.warning("pdf text extraction failed: %s", type(e).__name__)
        return []


def is_scanned(pages: list[str]) -> bool:
    text = [p for p in pages if p.strip()]
    return not pages or len(text) < max(1, len(pages) // 4) or sum(len(p) for p in text) < 500


def _nature_of(page: str) -> str:
    low = page.lower()
    if re.search(r"consolidated\s+(statement|financial results|audited|unaudited|results)", low):
        return "consolidated"
    if re.search(r"standalone\s+(statement|financial results|audited|unaudited|results)", low):
        return "standalone"
    return "unclear"


# Statement lines as letters only, so OCR'd text ("Revenuefrom Operations", "Profit/(Loss) before Tax") matches too.
LINE_KEYS = {"revenuefromoperations": 2, "interestearned": 2, "totalincome": 2, "profitbeforetax": 3,
             "profitlossbeforetax": 3, "taxexpense": 2, "netprofit": 2, "profitfortheperiod": 2,
             "profitlossfortheperiod": 2, "earningsper": 3, "paidup": 2}
PERIOD_KEYS = ("threemonthsended", "quarterended", "quarterending", "quarterandyearended")


def _letters(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalpha())


def page_score(page: str) -> int:
    t = _letters(page)
    score = 0
    for k, w in LINE_KEYS.items():
        if k in t and not (k == "profitbeforetax" and "profitlossbeforetax" in t)                 and not (k == "profitfortheperiod" and "profitlossfortheperiod" in t):
            score += w
    return score + (3 if any(k in t for k in PERIOD_KEYS) else 0) + (2 if "statementof" in t else 0)


def select_pages(pages: list[str], nature: str) -> tuple[str, list[int], str]:
    """The best statement page for the wanted nature (plus the next page if the statement runs over).
    Returns (text, 1-based page numbers, nature found)."""
    scored = [(page_score(p), i, _nature_of(p)) for i, p in enumerate(pages)]
    candidates = [x for x in scored if x[0] >= 9]
    if not candidates:
        return "", [], "none"
    exact = [x for x in candidates if x[2] == nature]
    unclear = [x for x in candidates if x[2] == "unclear"]
    pool = exact or unclear or candidates
    best = max(pool, key=lambda x: (x[0], -x[1]))
    chosen = [best[1]]
    nxt = best[1] + 1
    if nxt < len(pages) and "earningsper" not in _letters(pages[best[1]]) and "earningsper" in _letters(pages[nxt]):
        chosen.append(nxt)
    text = "\n".join(f"--- page {i + 1} ---\n{pages[i]}" for i in chosen)[:MAX_CHARS]
    return text, [i + 1 for i in chosen], best[2]


# -- scanned pages ------------------------------------------------------------------------------------------
SCANNED_PAGE_CHARS = 200
MAX_OCR_PAGES = 40
NOT_THE_STATEMENT = ("notesto", "assetsandliabilities", "cashflow", "auditor", "independent",
                     "regulation52", "deviation", "segment", "charteredaccountants")
OCR_MIN_SCORE = 12          # an OCR'd page must show most statement lines, not just mention a few


def statement_heading(text: str) -> tuple[bool, str]:
    """Does an OCR'd heading band announce a quarterly results statement? (OCR often drops spaces.)"""
    n = "".join(text.lower().split())
    ok = "financialresult" in n and ("statementof" in n or "quarterended" in n or "quarterand" in n) \
        and not any(x in n for x in NOT_THE_STATEMENT)
    nature = "consolidated" if "consolidated" in n else "standalone" if "standalone" in n else "unclear"
    return ok, nature


def ocr_candidates(text_pages: list[str], n_pages: int) -> list[int]:
    """Image-only pages first, then pages whose text layer doesn't read as a statement (often a bad embedded OCR)."""
    if not text_pages:
        return list(range(n_pages))[:MAX_OCR_PAGES]
    blank = [i for i, t in enumerate(text_pages) if len(t.strip()) < SCANNED_PAGE_CHARS]
    murky = [i for i, t in enumerate(text_pages) if i not in blank and page_score(t) < 9]
    return (blank + murky)[:MAX_OCR_PAGES]


def find_scanned_statement(data: bytes, text_pages: list[str], nature: str, ocr) -> Optional[dict[str, Any]]:
    """OCR heading bands until the wanted statement turns up (stop there), confirm with a full-page OCR,
    and return the transcript plus page images for the model."""
    from .ocr import open_pdf

    doc, digest = open_pdf(data)
    fallback = None
    for i in ocr_candidates(text_pages, len(doc)):
        ok, nat = statement_heading(ocr.heading(doc, digest, i))
        if not ok or nat not in (nature, "unclear"):
            continue
        full = ocr.full(doc, digest, i)
        if page_score(full) < OCR_MIN_SCORE:
            continue
        used, texts = [i], [full]
        if "earningsper" not in _letters(full) and i + 1 < len(doc):
            nxt = text_pages[i + 1] if i + 1 < len(text_pages) and page_score(text_pages[i + 1]) >= 5 else ocr.full(doc, digest, i + 1)
            if "earningsper" in _letters(nxt):
                used.append(i + 1)
                texts.append(nxt)
        found = {"text": "(OCR transcript of scanned pages; read the values from the attached page images)\n"
                         + "\n".join(f"--- page {p + 1} ---\n{t}" for p, t in zip(used, texts))[:MAX_CHARS],
                 "pages": [p + 1 for p in used], "nature": nat, "images": [ocr.png(doc, p) for p in used]}
        if nat == nature:
            return found
        fallback = fallback or found                 # heading didn't say which; keep looking for an explicit one
    return fallback


# -- which quarters are missing ----------------------------------------------------------------------------
@dataclass
class Gap:
    symbol: str
    instrument_id: int
    period_end: date
    nature: str


def quarter_ends(today: date, n: int) -> list[date]:
    """The last n quarter-end dates before today."""
    ends = [date(y, mth, d) for y in range(today.year - (n // 4 + 2), today.year + 1)
            for mth, d in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return [d for d in ends if d < today][-n:]


def find_gaps(s: Session, today: date, quarters: int = 8, universe: str = "NIFTY200",
              symbols: Optional[list[str]] = None) -> list[Gap]:
    """Missing quarters that should exist: past their results due date and after the stock's first trading day.
    Insurers are skipped (their statements have no revenue line in this sense)."""
    q = select(m.Instrument).join(m.UniverseMembership).where(m.UniverseMembership.universe == universe,
                                                              m.UniverseMembership.valid_to.is_(None))
    if symbols:
        q = select(m.Instrument).where(m.Instrument.symbol.in_([x.upper() for x in symbols]))
    insts = s.scalars(q).all()
    ids = [i.id for i in insts]
    have: dict[int, set[date]] = {}
    sources: dict[int, set[str]] = {}
    for iid, pe, src in s.execute(select(m.FundamentalSnapshot.instrument_id, m.FundamentalSnapshot.period_end,
                                         m.FundamentalSnapshot.source).where(m.FundamentalSnapshot.instrument_id.in_(ids))):
        have.setdefault(iid, set()).add(pe)
        sources.setdefault(iid, set()).add(src)
    first = dict(s.execute(select(m.PriceDaily.instrument_id, func.min(m.PriceDaily.trade_date))
                           .where(m.PriceDaily.instrument_id.in_(ids)).group_by(m.PriceDaily.instrument_id)).all())
    gaps = []
    for inst in insts:
        if "insurance" in (inst.industry or "").lower() or "insurance" in (inst.name or "").lower():
            continue
        nature = "standalone" if sources.get(inst.id) and all("standalone" in x for x in sources[inst.id]) else "consolidated"
        for pe in quarter_ends(today, quarters):
            listed = first.get(inst.id)
            if pe in have.get(inst.id, set()) or results_due(pe) >= today or (listed and pe <= listed):
                continue
            gaps.append(Gap(inst.symbol, inst.id, pe, nature))
    return gaps


# -- one gap ------------------------------------------------------------------------------------------------
@dataclass
class PdfReport:
    tried: int = 0
    stored: int = 0
    review: int = 0
    dry_run: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def fill_gap(deps, f: sessionmaker[Session], src: NseAnnouncements, gap: Gap, decision_time: datetime,
                   dry_run: bool = False, ocr=None) -> dict[str, Any]:
    from .filing_text import extract_from_text

    docs = src.result_pdfs(gap.symbol, gap.period_end)
    last: dict[str, Any] = {"status": "no_pdf"}
    for doc in docs[:3]:
        data = src.pdf(doc)
        pages = pdf_pages(data)
        images = None
        if is_scanned(pages):
            last = {"status": "scanned", "url": doc.url}
            text, used, found = "", [], "none"
        else:
            text, used, found = select_pages(pages, gap.nature)
            if not text:
                last = {"status": "no_statement", "url": doc.url}
            elif found in ("consolidated", "standalone") and found != gap.nature:
                # Only the other statement is readable (the wanted one is often a scanned page). Mixing standalone
                # into a consolidated series would distort growth, so don't spend an LLM call on it.
                last = {"status": f"only_{found}_readable", "url": doc.url, "pages": used}
                text = ""
        if not text and ocr is not None:
            hit = find_scanned_statement(data, pages, gap.nature, ocr)
            if hit is None:
                last = {**last, "status": last["status"] + "+ocr_not_found"}
                continue
            text, used, found, images = hit["text"], hit["pages"], hit["nature"], hit["images"]
        if not text:
            continue
        ocr_used = images is not None
        if images is None and ocr is not None:
            # Text-layer PDFs too: the model reads the page images, the text is a guide. Some "text" is the
            # issuer's own poor OCR, and images also fix column ambiguity. A few cents' worth of tokens.
            from .ocr import open_pdf

            try:
                pdf_doc, _ = open_pdf(data)
                images = [ocr.png(pdf_doc, p - 1) for p in used]
            except Exception as e:                    # rendering is a bonus here; the text path still works
                log.warning("could not render %s pages %s: %s", doc.url, used, type(e).__name__)
                images = None
        info = {"url": doc.url, "pages": used, "nature_found": found, "chars": len(text),
                "published_at": doc.published_at.isoformat(), "ocr": ocr_used, "images": len(images or [])}
        if dry_run:
            return {"status": "dry_run", **info}
        out = await extract_from_text(deps, f, gap.symbol, gap.period_end, text, doc.published_at.date(), decision_time,
                                      nature=gap.nature, images=images, ocr=ocr_used,
                                      source_ref={"url": doc.url, "pages": used, "desc": doc.desc, "ocr": ocr_used})
        return {**out, **info}
    return last


async def fill_gaps(deps, f: sessionmaker[Session], src: NseAnnouncements, gaps: list[Gap], decision_time: datetime,
                    dry_run: bool = False, limit: Optional[int] = None, ocr=None) -> tuple[PdfReport, list[dict[str, Any]]]:
    rep, rows = PdfReport(), []
    for g in gaps[:limit] if limit else gaps:
        rep.tried += 1
        try:
            out = await fill_gap(deps, f, src, g, decision_time, dry_run, ocr)
        except Exception as e:
            log.exception("pdf gap %s %s", g.symbol, g.period_end)
            rep.errors.append(f"{g.symbol} {g.period_end}: {type(e).__name__}: {e}"[:200])
            continue
        status = out.get("status")
        rows.append({"symbol": g.symbol, "period_end": g.period_end.isoformat(), **out})
        if status == "stored":
            rep.stored += 1
        elif status == "review":
            rep.review += 1
        elif status == "dry_run":
            rep.dry_run += 1
        else:
            rep.skipped.append(f"{g.symbol} {g.period_end}: {status}")
    return rep, rows


class ChainedResults:
    """NSE announcements first, then BSE (for companies that file only there, e.g. MCX). Same interface as
    NseAnnouncements: result_pdfs(symbol, period_end) and pdf(doc)."""

    def __init__(self, nse: NseAnnouncements, bse=None, isin_of=None):
        self.nse, self.bse, self.isin_of = nse, bse, isin_of or (lambda symbol: None)

    def result_pdfs(self, symbol: str, period_end: date):
        docs = self.nse.result_pdfs(symbol, period_end)
        if docs or self.bse is None:
            return docs
        return self.bse.result_pdfs(symbol, period_end, self.isin_of(symbol))

    def pdf(self, doc):
        return (self.bse if self.bse is not None and "bseindia.com" in doc.url else self.nse).pdf(doc)


def isin_lookup(f: sessionmaker[Session]):
    def get(symbol: str) -> Optional[str]:
        with f() as s:
            return s.scalar(select(m.Instrument.isin).where(m.Instrument.symbol == symbol))
    return get
