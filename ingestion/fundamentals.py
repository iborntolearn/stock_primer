"""Quarterly fundamentals from NSE XBRL into fundamental_snapshot (feature: filing extraction, phase 4).

For each symbol: list filings, prefer the consolidated statement per quarter, parse
the XBRL, run deterministic checks (the doc's LLM-15 validation rules applied to
the filed numbers), then store in ₹ crore with published_at = broadcast date, so
every downstream read is point-in-time. Filings that fail a check are not stored;
they raise a data_anomaly review task instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m

from .adapters.nse_results import NseResults, ResultFiling, balance_sheet, parse_xbrl
from .reference import upsert_instrument

log = logging.getLogger(__name__)
CRORE = Decimal("10000000")
IST = timezone(timedelta(hours=5, minutes=30))


def crore(x: Optional[Decimal]) -> Optional[Decimal]:
    return None if x is None else (x / CRORE).quantize(Decimal("0.01"))


EPS_ISSUE = "EPS"
UNSUPPORTED = "revenue or profit missing"     # e.g. insurers, whose filings use a different statement format


def reconcile_owners(q: dict[str, Any]) -> dict[str, Any]:
    """Some filings tag a different figure as 'profit attributable to owners' (e.g. ₹9.5 cr against ₹1,107 cr total).
    Keep it only if owners + non-controlling interests adds back to total profit; otherwise fall back to the total."""
    pat, owners = q.get("pat"), q.get("pat_owners")
    if pat is None or owners is None:
        return q
    if abs(owners + (q.get("nci") or 0) - pat) > max(Decimal("0.02") * abs(pat), Decimal(1e6)):
        return {**q, "pat_owners": None, "owners_note": f"filed owners' profit {owners} does not reconcile with total {pat}; using total"}
    return q


def check_statement(q: dict[str, Any], prior_year: Optional[dict[str, Any]] = None) -> list[str]:
    """Deterministic checks; any message means 'do not store, ask a human'. Call reconcile_owners first."""
    issues = []
    rev, pat, pbt, tax = q.get("revenue"), q.get("pat"), q.get("pbt"), q.get("tax")
    if rev is None or pat is None:
        issues.append(UNSUPPORTED)
        return issues
    if rev < 0:
        issues.append("negative revenue")
    if q.get("income") is not None and q.get("other_income") is not None:
        if abs(q["income"] - (rev + q["other_income"])) > max(Decimal("0.01") * abs(q["income"]), Decimal(1e5)):
            issues.append("total income != revenue + other income")
    if pbt is not None and tax is not None:
        # Consolidated PAT = PBT - tax, plus whichever of these the filer reports below the tax line: share of
        # associates, discontinued operations, regulatory deferral movements (utilities). Accept any combination;
        # allow 10% of PBT for smaller reconciling items.
        from itertools import combinations

        extras = [q.get(k) for k in ("associates", "discontinued", "regulatory") if q.get(k)]
        candidates = {pbt - tax + sum(c, Decimal(0)) for n in range(len(extras) + 1) for c in combinations(extras, n)}
        tol = max(Decimal("0.10") * abs(pbt), Decimal(1e6))
        if all(abs(c - pat) > tol for c in candidates):
            issues.append("profit after tax inconsistent with PBT - tax")
    eps, cap, fv = q.get("eps_basic"), q.get("paid_up_capital"), q.get("face_value")
    if eps not in (None, 0) and cap and fv:
        shares = cap / fv
        attributable = q.get("pat_owners") or pat
        implied = attributable / shares if shares else None
        if implied is not None and abs(implied - eps) > max(Decimal("0.15") * abs(eps), Decimal("0.05")):
            issues.append(f"{EPS_ISSUE} {eps} differs from profit/shares {implied:.2f}")
    if prior_year and prior_year.get("pat") not in (None, 0) and prior_year.get("revenue"):
        yoy_rev = float(rev / prior_year["revenue"] - 1) if prior_year["revenue"] else 0.0
        if abs(yoy_rev) > 2.0:
            issues.append(f"revenue YoY {yoy_rev:+.0%} beyond ±200%")
    return issues


def to_snapshot(iid: int, f: ResultFiling, q: dict[str, Any]) -> m.FundamentalSnapshot:
    rev = crore(q["revenue"])
    ebitda = None
    if q.get("is_insurer"):
        ebitda = None                                  # no EBITDA concept; insurer ratios are kept in raw instead
    elif q.get("is_bank") and q.get("bank_operating_profit") is not None:
        ebitda = crore(q["bank_operating_profit"])          # banks: pre-provision operating profit
    elif q.get("pbt") is not None:
        ebitda = crore(q["pbt"] + (q.get("finance_costs") or 0) + (q.get("depreciation") or 0)
                       - (q.get("other_income") or 0) - (q.get("exceptional") or 0))
    base = crore((q["revenue"] or 0) + (q.get("other_income") or 0)) if q.get("is_bank") else rev
    op_margin = (ebitda / base * 100).quantize(Decimal("0.01")) if ebitda is not None and base else None
    published = f.published_at.replace(tzinfo=IST).date()
    bs = balance_sheet(q)
    ok_bs = bs is not None and not bs.get("invalid")
    return m.FundamentalSnapshot(
        instrument_id=iid, period_end=q.get("period_end") or f.period_end, period_type="Q", published_at=published,
        revenue=rev, ebitda=ebitda, pat=crore(q.get("pat_owners") or q["pat"]), eps=q.get("eps_basic"),
        op_margin=op_margin, equity=crore(bs["equity_owners"]) if ok_bs else None,
        total_debt=crore(bs["debt"]) if ok_bs and bs["debt"] is not None else None,
        raw={k: str(v) if v is not None else None for k, v in q.items()} | {
            "consolidated": f.consolidated, "xbrl": f.xbrl_url, "listing": f.source,
            "published_at": f.published_at.isoformat(), "bs_kind": bs["kind"] if bs else None,
            "bs_invalid": bool(bs and bs.get("invalid"))},
        source=f"nse_xbrl_{'consolidated' if f.consolidated else 'standalone'}")


@dataclass
class FundamentalsReport:
    symbols: int = 0
    stored: int = 0
    skipped_existing: int = 0
    anomalies: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def ingest_symbol(f: sessionmaker[Session], src: NseResults, symbol: str, quarters: int = 8,
                  rep: Optional[FundamentalsReport] = None) -> FundamentalsReport:
    rep = rep or FundamentalsReport()
    rep.symbols += 1
    try:
        filings = src.filings(symbol)
    except Exception as e:
        rep.errors.append(f"{symbol}: listing {type(e).__name__}")
        return rep
    by_q: dict[date, ResultFiling] = {}
    for x in sorted(filings, key=lambda x: (x.period_end, x.consolidated, x.published_at)):
        cur = by_q.get(x.period_end)
        if cur is None or (x.consolidated and not cur.consolidated) or (x.consolidated == cur.consolidated and x.published_at > cur.published_at):
            by_q[x.period_end] = x                       # prefer consolidated, then the latest (revised) filing
    chosen = sorted(by_q.values(), key=lambda x: x.period_end)[-quarters:]
    with f() as s, s.begin():
        iid = upsert_instrument(s, symbol)
        have = {(r.period_end, r.source) for r in s.scalars(select(m.FundamentalSnapshot).where(
            m.FundamentalSnapshot.instrument_id == iid, m.FundamentalSnapshot.period_type == "Q"))}
    parsed: dict[date, dict] = {}
    for x in chosen:
        source = f"nse_xbrl_{'consolidated' if x.consolidated else 'standalone'}"
        if (x.period_end, source) in have:
            rep.skipped_existing += 1
            continue
        try:
            parsed[x.period_end] = (x, reconcile_owners(parse_xbrl(src.xbrl(x))))
        except Exception as e:
            rep.errors.append(f"{symbol} {x.period_end}: xbrl {type(e).__name__}")
    with f() as s, s.begin():
        for pe, (x, q) in sorted(parsed.items()):
            prior = parsed.get(pe.replace(year=pe.year - 1))
            issues = check_statement(q, prior[1] if prior else None)
            if issues == [UNSUPPORTED]:
                rep.errors.append(f"{symbol} {pe}: unsupported statement format (no revenue/profit lines)")
                continue                              # nothing a reviewer can fix; reported, not queued
            eps_only = [i for i in issues if i.startswith(EPS_ISSUE + " ")]
            if eps_only and len(eps_only) == len(issues):
                # Revenue and profit pass; only EPS disagrees with profit/shares (often a share-count change in the
                # quarter). Store the quarter without EPS rather than lose it, and say why.
                q = {**q, "eps_basic": None, "eps_note": eps_only[0]}
                rep.anomalies.append(f"{symbol} {pe}: stored without EPS ({eps_only[0]})")
                issues = []
            if issues:
                rep.anomalies.append(f"{symbol} {pe}: {'; '.join(issues)}")
                s.add(m.HitlTask(task_type="data_anomaly", subject_type="instrument", subject_id=iid, priority=3,
                                 payload={"check": "fundamentals", "symbol": symbol, "period_end": pe.isoformat(),
                                          "issues": issues, "xbrl": x.xbrl_url},
                                 ai_recommendation="exclude", reason=f"{symbol} {pe} results: {'; '.join(issues)}",
                                 default_on_expiry="exclude_instrument"))
                continue
            s.add(to_snapshot(iid, x, q))
            rep.stored += 1
    return rep


def ingest_many(f: sessionmaker[Session], src: NseResults, symbols: list[str], quarters: int = 8) -> FundamentalsReport:
    rep = FundamentalsReport()
    for sym in symbols:
        ingest_symbol(f, src, sym, quarters, rep)
    return rep


def symbols_with_recent_results(s: Session, today: date, days: int = 3) -> list[str]:
    """Symbols whose board met for results in the last few days (from the results calendar)."""
    rows = s.execute(select(m.Instrument.symbol).join(m.CorporateAction, m.CorporateAction.instrument_id == m.Instrument.id)
                     .where(m.CorporateAction.action_type == "results", m.CorporateAction.ex_date <= today,
                            m.CorporateAction.ex_date >= today - timedelta(days=days))).all()
    return sorted({r for (r,) in rows})


def backfill_balance_sheets(f: sessionmaker[Session], src: NseResults) -> dict[str, int]:
    """Fill equity/debt on stored XBRL snapshots from their (cached) XBRL. Re-fetches only if not cached."""
    with f() as s:
        rows = s.execute(select(m.FundamentalSnapshot.id, m.Instrument.symbol, m.FundamentalSnapshot.period_end,
                                m.FundamentalSnapshot.raw, m.FundamentalSnapshot.source)
                         .join(m.Instrument, m.Instrument.id == m.FundamentalSnapshot.instrument_id)
                         .where(m.FundamentalSnapshot.source.like("nse_xbrl_%"), m.FundamentalSnapshot.equity.is_(None))).all()
    out = {"checked": 0, "filled": 0, "no_balance_sheet": 0, "invalid": 0, "errors": 0}
    for sid, sym, pe, raw, source in rows:
        raw = raw or {}
        if not raw.get("xbrl"):
            continue
        out["checked"] += 1
        fil = ResultFiling(sym, pe, "consolidated" in source, datetime.fromisoformat(raw["published_at"]), raw["xbrl"],
                           raw.get("listing", "nse_legacy"))
        try:
            q = parse_xbrl(src.xbrl(fil))
        except Exception:
            out["errors"] += 1
            continue
        bs = balance_sheet(q)
        if bs is None:
            out["no_balance_sheet"] += 1
            continue
        with f() as s, s.begin():
            row = s.get(m.FundamentalSnapshot, sid)
            extra = {k: str(v) if v is not None else None for k, v in q.items() if k.startswith("bs_")}
            row.raw = {**raw, **extra, "bs_kind": bs["kind"], "bs_invalid": bool(bs.get("invalid"))}
            if bs.get("invalid"):
                out["invalid"] += 1
                continue
            row.equity = crore(bs["equity_owners"])
            row.total_debt = crore(bs["debt"]) if bs["debt"] is not None else None
            out["filled"] += 1
    return out
