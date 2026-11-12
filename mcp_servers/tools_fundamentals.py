"""fundamentals-mcp: quarterly statements and half-yearly balance sheets from NSE XBRL, and quarterly
shareholding patterns, all point-in-time by publication date. Filing text search is not built yet.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Optional

from sqlalchemy import select

from core import models as m
from quant.fundamentals import last_close, load_snapshots, metrics, pe_history, sector_medians

from .registry import IST, Out, ToolFailure, ctx, decision_date, decision_dt, tool

NO_SOURCE = "no published quarterly results for {sym} in the store (run the fundamentals ingest)"
BS_NOTE = {"bank": "bank: debt/equity and ROCE don't apply; leverage shown as assets/equity",
           "nbfc": "lender: debt/equity and ROCE don't apply; leverage shown as assets/equity",
           "insurer": "insurer: revenue is net (earned) premium; margins, debt/equity and ROCE don't apply - see `insurer` "
                      "for premium growth, solvency and combined/claims/expense ratios"}


def _inst(s, symbol: str) -> m.Instrument:
    inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == symbol.upper()))
    if inst is None:
        raise ToolFailure("NOT_FOUND", f"unknown symbol {symbol}")
    return inst


def _sector_peers(s, inst: m.Instrument) -> list[int]:
    if inst.sector_id is None:
        return []
    return list(s.scalars(select(m.Instrument.id).where(m.Instrument.sector_id == inst.sector_id, m.Instrument.is_active)))


def _num(x):
    return None if x is None else float(x)


@tool("fundamentals")
def get_financials(symbol: str, period_type: str = "Q", n_periods: int = 8, as_of: str = "") -> Out:
    """Quarterly statement lines (₹ crore): revenue, EBITDA (banks: pre-provision operating profit), net profit, EPS."""
    d = decision_date(as_of)
    with ctx().factory() as s:
        inst = _inst(s, symbol)
        snaps = load_snapshots(s, [inst.id], d, quarters=min(int(n_periods), 12)).get(inst.id, [])
    if not snaps:
        raise ToolFailure("NOT_FOUND", NO_SOURCE.format(sym=symbol.upper()))
    return Out({"symbol": symbol.upper(), "unit": "INR crore", "basis": snaps[0].source, "periods": [
        {"period_end": r.period_end.isoformat(), "published_at": r.published_at.isoformat(), "revenue": _num(r.revenue),
         "ebitda": _num(r.ebitda), "pat": _num(r.pat), "eps": _num(r.eps), "op_margin_pct": _num(r.op_margin)}
        for r in snaps]}, "fundamental", snaps[0].source, as_of=datetime.combine(snaps[0].published_at, time(18), IST))


@tool("fundamentals")
def get_ratios(symbol: str, as_of: str = "") -> Out:
    """Margins, YoY growth, earnings consistency and trailing EPS, with sector medians."""
    d = decision_date(as_of)
    with ctx().factory() as s:
        inst = _inst(s, symbol)
        snaps = load_snapshots(s, [inst.id], d).get(inst.id, [])
        if not snaps:
            raise ToolFailure("NOT_FOUND", NO_SOURCE.format(sym=symbol.upper()))
        mt = metrics(snaps, last_close(s, inst.id, d))
        med = sector_medians(s, _sector_peers(s, inst), d, kind=mt.get("balance_sheet_kind"))
    for k in ("pe", "earnings_yield_pct", "pb"):
        mt.pop(k, None)
    notes = []
    if not mt.get("balance_sheet_as_of"):
        notes.append("no balance sheet published yet in the stored filings (ROE/ROCE/debt unavailable)")
    elif mt.get("negative_equity"):
        notes.append("negative equity (accumulated losses): ROE and debt/equity are not meaningful")
    elif BS_NOTE.get(mt.get("balance_sheet_kind")):
        notes.append(BS_NOTE[mt["balance_sheet_kind"]])
    return Out({"symbol": symbol.upper(), **mt,
                "sector_median": {k: med.get(k) for k in ("op_margin_pct", "pat_margin_pct", "revenue_yoy_pct", "roe_pct",
                                                          "roce_pct", "debt_equity")},
                "sector_peers_with_data": med.get("peers"), "notes": notes}, "fundamental", snaps[0].source,
               as_of=datetime.combine(snaps[0].published_at, time(18), IST))


@tool("fundamentals")
def get_valuation(symbol: str, as_of: str = "") -> Out:
    """Trailing P/E versus the sector median and the stock's own recent history."""
    d = decision_date(as_of)
    with ctx().factory() as s:
        inst = _inst(s, symbol)
        snaps = load_snapshots(s, [inst.id], d, quarters=12).get(inst.id, [])
        if not snaps:
            raise ToolFailure("NOT_FOUND", NO_SOURCE.format(sym=symbol.upper()))
        price = last_close(s, inst.id, d)
        mt = metrics(snaps, price)
        if mt.get("pe") is None:
            raise ToolFailure("NOT_FOUND", f"{symbol.upper()}: trailing P/E needs four quarters with positive earnings")
        hist = pe_history(s, snaps, inst.id)
        med = sector_medians(s, _sector_peers(s, inst), d, kind=mt.get("balance_sheet_kind"))
    pct = round(100 * sum(1 for h in hist if h < mt["pe"]) / len(hist)) if len(hist) >= 3 else None
    return Out({"symbol": symbol.upper(), "price": price, "ttm_eps": mt["ttm_eps"], "pe": mt["pe"],
                "earnings_yield_pct": mt["earnings_yield_pct"], "sector_median_pe": med.get("pe"),
                "pb": mt.get("pb"), "sector_median_pb": med.get("pb"), "book_as_of": mt.get("balance_sheet_as_of"),
                "own_history_pe": hist, "own_history_percentile": pct,
                "note": "EV/EBITDA not computed (no reliable EBITDA for every format)"}, "fundamental", snaps[0].source)


SH_FIELDS = ("promoter_pct", "public_pct", "fpi_pct", "dii_pct", "mf_pct", "insurance_pct", "retail_pct",
             "government_pct", "promoter_encumbered_pct", "promoter_pledged_pct")


@tool("fundamentals")
def get_shareholding(symbol: str, n_quarters: int = 6, as_of: str = "") -> Out:
    """Promoter, FPI, DII, mutual-fund and retail holding by quarter, with changes and promoter pledge/encumbrance.
    Percentages are of total shares, except encumbered/pledged, which are % of the promoter group's holding."""
    when = decision_dt(as_of)
    with ctx().factory() as s:
        inst = _inst(s, symbol)
        rows = s.scalars(select(m.Shareholding).where(m.Shareholding.instrument_id == inst.id,
                                                      m.Shareholding.published_at <= when)
                         .order_by(m.Shareholding.period_end.desc()).limit(max(1, min(int(n_quarters), 12)))).all()
    if not rows:
        raise ToolFailure("NOT_FOUND", f"no shareholding pattern for {symbol.upper()} published by {when.date()} (run the shareholding ingest)")
    q = [{"period_end": r.period_end.isoformat(), "published_at": r.published_at.astimezone(IST).date().isoformat(),
          **{k: _num(getattr(r, k)) for k in SH_FIELDS}, "shareholders": r.shareholders, "revised": r.revised} for r in rows]
    latest, oldest = q[0], q[-1]

    def chg(k, a, b):
        return None if a.get(k) is None or b.get(k) is None else round(a[k] - b[k], 2)
    changes = {"vs_previous_quarter": {k: chg(k, q[0], q[1]) for k in ("promoter_pct", "fpi_pct", "dii_pct", "mf_pct", "retail_pct",
                                                                      "promoter_encumbered_pct")} if len(q) > 1 else None,
               f"over_{len(q)}_quarters": {k: chg(k, latest, oldest) for k in ("promoter_pct", "fpi_pct", "dii_pct", "mf_pct",
                                                                               "promoter_encumbered_pct")} if len(q) > 1 else None}
    flags = []
    if (latest.get("promoter_encumbered_pct") or 0) >= 25:
        flags.append(f"{latest['promoter_encumbered_pct']:.1f}% of promoter shares encumbered")
    vq = changes["vs_previous_quarter"] or {}
    if (vq.get("promoter_pct") or 0) <= -1:
        flags.append(f"promoter holding fell {vq['promoter_pct']:.2f} pp last quarter")
    if (vq.get("promoter_encumbered_pct") or 0) >= 5:
        flags.append(f"promoter encumbrance rose {vq['promoter_encumbered_pct']:.1f} pp last quarter")
    return Out({"symbol": symbol.upper(), "quarters": q, "changes": changes, "flags": flags,
                "unit": "% of total shares; encumbered/pledged are % of promoter shares"},
               "filing", "nse_shareholding", as_of=rows[0].published_at)


@tool("fundamentals")
def search_filings(symbol: str, query: str, from_date: str = "", to_date: str = "", as_of: str = "") -> Out:
    """Semantic search over exchange filings (untrusted text)."""
    raise ToolFailure("NOT_FOUND", f"filing text search is not built yet for {symbol.upper()}")


@tool("fundamentals")
def get_filing_excerpt(doc_ref: str, page_range: str = "", as_of: str = "") -> Out:
    """Bounded excerpt of a filing (untrusted text)."""
    raise ToolFailure("NOT_FOUND", "filing text store not built yet")
