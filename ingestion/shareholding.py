"""Shareholding patterns into the `shareholding` table (point-in-time by broadcast date).

Checks before storing: categories add up (promoter + public + non-promoter-non-public ≈ 100%),
institutions don't exceed the public float, and the XBRL agrees with the exchange's headline
promoter %. A failing filing is reported and skipped; the earlier quarters stay usable.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m

from .adapters.nse_shareholding import NseShareholding, ShpFiling

IST = timezone(timedelta(hours=5, minutes=30))
TOL = Decimal("1.0")                 # percentage points


def check_pattern(sh: dict[str, Any], f: ShpFiling) -> list[str]:
    issues = []
    p, pub = sh.get("promoter_pct"), sh.get("public_pct")
    if p is None or pub is None:
        return ["promoter or public holding missing"]
    for k in ("promoter_pct", "public_pct", "fpi_pct", "dii_pct", "mf_pct", "promoter_encumbered_pct", "promoter_pledged_pct"):
        v = sh.get(k)
        if v is not None and not (0 <= v <= 100):
            issues.append(f"{k} {v} outside 0-100")
    total = p + pub + (sh.get("non_promoter_non_public_pct") or 0)
    if abs(total - 100) > TOL:
        issues.append(f"categories sum to {total}%")
    inst = (sh.get("fpi_pct") or 0) + (sh.get("dii_pct") or 0)
    if inst > pub + TOL:
        issues.append(f"institutions {inst}% exceed the public float {pub}%")
    if f.promoter_pct is not None and abs(f.promoter_pct - p) > TOL:
        issues.append(f"XBRL promoter {p}% vs exchange summary {f.promoter_pct}%")
    return issues


@dataclass
class ShareholdingReport:
    symbols: int = 0
    stored: int = 0
    updated: int = 0
    skipped_existing: int = 0
    issues: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def ingest_symbol(f: sessionmaker[Session], src: NseShareholding, symbol: str, quarters: int = 8,
                  rep: Optional[ShareholdingReport] = None, bse=None, today: Optional[date] = None) -> ShareholdingReport:
    rep = rep or ShareholdingReport()
    rep.symbols += 1
    try:
        filings = src.filings(symbol)
    except Exception as e:
        rep.errors.append(f"{symbol}: listing {type(e).__name__}")
        return rep
    if not filings and bse is not None:
        return ingest_bse(f, bse, symbol, quarters, rep, today)
    with f() as s:
        inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == symbol))
        if inst is None:
            rep.errors.append(f"{symbol}: unknown instrument")
            return rep
        iid, isin = inst.id, inst.isin
        have = {r.period_end: r for r in s.scalars(select(m.Shareholding).where(m.Shareholding.instrument_id == iid))}
        have = {pe: (r.published_at, r.id) for pe, r in have.items()}
    # Main equity line only (partly-paid or other ISINs file separately).
    main_isin = isin if isin and any(x.isin == isin for x in filings) else (
        Counter(x.isin for x in filings).most_common(1)[0][0] if filings else None)
    latest: dict[Any, ShpFiling] = {}
    for x in filings:
        if x.isin == main_isin and (x.period_end not in latest or x.published_at > latest[x.period_end].published_at):
            latest[x.period_end] = x
    for pe in sorted(latest)[-quarters:]:
        x = latest[pe]
        pub = x.published_at.replace(tzinfo=IST)
        prev = have.get(pe)
        if prev is not None and prev[0] >= pub:
            rep.skipped_existing += 1
            continue
        try:
            sh = src.summary(x)
        except Exception as e:
            rep.errors.append(f"{symbol} {pe}: {type(e).__name__}")
            continue
        issues = check_pattern(sh, x)
        if issues:
            rep.issues.append(f"{symbol} {pe}: {'; '.join(issues)}")
            continue
        cols = {k: sh.get(k) for k in ("promoter_pct", "public_pct", "fpi_pct", "dii_pct", "mf_pct", "insurance_pct",
                                       "retail_pct", "government_pct", "promoter_encumbered_pct", "promoter_pledged_pct",
                                       "shareholders")}
        raw = {"xbrl": x.xbrl, "isin": x.isin, "date_of_report": sh.get("date_of_report"),
               "no_promoter_group": bool(sh.get("no_promoter_group")),
               "non_promoter_non_public_pct": str(sh.get("non_promoter_non_public_pct"))}
        with f() as s, s.begin():
            if prev is not None:                          # a revision: replace, and only "known" from its broadcast
                row = s.get(m.Shareholding, prev[1])
                for k, v in cols.items():
                    setattr(row, k, v)
                row.published_at, row.revised, row.raw = pub, True, raw
                rep.updated += 1
            else:
                s.add(m.Shareholding(instrument_id=iid, period_end=pe, published_at=pub, revised=x.revised, raw=raw, **cols))
                rep.stored += 1
    return rep


def ingest_many(f: sessionmaker[Session], src: NseShareholding, symbols: list[str], quarters: int = 8,
                progress=None, bse=None) -> ShareholdingReport:
    rep = ShareholdingReport()
    for i, sym in enumerate(symbols, 1):
        before = rep.stored + rep.updated
        ingest_symbol(f, src, sym, quarters, rep, bse=bse)
        if progress:
            progress(i, len(symbols), sym, rep.stored + rep.updated - before)
    return rep


def ingest_bse(f: sessionmaker[Session], bse, symbol: str, quarters: int, rep: ShareholdingReport,
               today: Optional[date] = None) -> ShareholdingReport:
    """Companies with no NSE shareholding filings (BSE-only filers): BSE's summary statement.
    Promoter / public / non-promoter-non-public only; dated at the SEBI deadline since BSE gives no timestamp."""
    from .adapters.bse import SHP_DEADLINE_DAYS
    from .filing_pdf import quarter_ends

    today = today or datetime.now(IST).date()
    with f() as s:
        inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == symbol))
        if inst is None:
            rep.errors.append(f"{symbol}: unknown instrument")
            return rep
        iid, isin = inst.id, inst.isin
        have = {r.period_end: (r.id, r.fpi_pct is None)
                for r in s.scalars(select(m.Shareholding).where(m.Shareholding.instrument_id == iid))}
    for pe in quarter_ends(today, quarters):
        if pe + timedelta(days=SHP_DEADLINE_DAYS) > today:
            continue
        if pe in have and not have[pe][1]:
            rep.skipped_existing += 1
            continue
        try:
            x = bse.shareholding(symbol, pe, isin)
        except Exception as e:
            rep.errors.append(f"{symbol} {pe}: BSE {type(e).__name__}")
            continue
        if x is None:
            rep.errors.append(f"{symbol} {pe}: no BSE shareholding summary")
            continue
        sh = dict(x.summary)
        sh["non_promoter_non_public_pct"] = (sh.get("trust_pct") or 0) + (sh.get("dr_pct") or 0) or None
        cat_total = sh.pop("categories_total_pct", None)
        if cat_total is not None and sh.get("public_pct") is not None and abs(cat_total - sh["public_pct"]) > TOL:
            rep.issues.append(f"{symbol} {pe} (BSE): categories add to {cat_total}% vs public {sh['public_pct']}%; categories dropped")
            for k in ("fpi_pct", "dii_pct", "mf_pct", "insurance_pct", "government_pct", "retail_pct"):
                sh[k] = None
        filing = ShpFiling(symbol, isin or "", pe, x.published_at, None, False, None, None)
        issues = check_pattern(sh, filing)
        if issues:
            rep.issues.append(f"{symbol} {pe} (BSE): {'; '.join(issues)}")
            continue
        cols = {k: sh.get(k) for k in ("promoter_pct", "public_pct", "fpi_pct", "dii_pct", "mf_pct", "insurance_pct",
                                       "government_pct", "retail_pct", "promoter_encumbered_pct", "shareholders")}
        raw = {"source": "bse_summary+categories" if cols["fpi_pct"] is not None else "bse_summary",
               "published_at_estimated": True, "no_promoter_group": bool(sh.get("no_promoter_group")),
               "non_promoter_non_public_pct": str(sh["non_promoter_non_public_pct"])}
        with f() as s, s.begin():
            if pe in have:                                   # fill the categories on a row stored before they were parsed
                row = s.get(m.Shareholding, have[pe][0])
                for k, v in cols.items():
                    setattr(row, k, v)
                row.raw = {**(row.raw or {}), **raw}
                rep.updated += 1
            else:
                s.add(m.Shareholding(instrument_id=iid, period_end=pe, published_at=x.published_at.replace(tzinfo=IST),
                                     raw=raw, source="bse_shp", **cols))
                rep.stored += 1
    return rep
