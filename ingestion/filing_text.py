"""LLM-15 fallback for results available only as text (PDF extracts): the model reads printed values,
then deterministic checks decide whether they are stored or sent to review.

Checks beyond the XBRL ones (check_statement): the model's confidence, the core lines all read, the right
quarter and the right statement (consolidated vs standalone), and a units check against stored
neighbouring quarters (a lakh/crore slip shows up as a ~100x jump).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from statistics import median
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m

from .fundamentals import check_statement, reconcile_owners
from .reference import upsert_instrument

UNITS = {"INR": Decimal(1), "INR_THOUSAND": Decimal(1e3), "INR_LAKH": Decimal(1e5), "INR_LAKHS": Decimal(1e5),
         "INR_MILLION": Decimal(1e6), "INR_CRORE": Decimal(1e7), "INR_CRORES": Decimal(1e7)}
MIN_CONFIDENCE = 0.8
MIN_CONFIDENCE_SCANNED = 0.85         # values read from page images of a scan
CRORE = Decimal(1e7)
REQUIRED = ("revenue", "profit_before_tax", "tax_expense", "net_profit")
NEIGHBOUR_RATIO = 3.0                  # revenue more than 3x away from adjacent quarters' median: suspect units


def to_rupees(lines, unit: str) -> Optional[dict[str, Any]]:
    mult = UNITS.get(unit.upper().replace(" ", "_").replace("RS", "INR").replace("₹", "INR"))
    if mult is None:
        return None

    def money(line):
        return None if line.value is None else Decimal(str(line.value)) * mult
    return {"revenue": money(lines.revenue), "other_income": money(lines.other_income), "income": money(lines.total_income),
            "pbt": money(lines.profit_before_tax), "tax": money(lines.tax_expense), "pat": money(lines.net_profit),
            "pat_owners": money(lines.net_profit_owners), "paid_up_capital": money(lines.paid_up_capital),
            "eps_basic": None if lines.eps_basic.value is None else Decimal(str(lines.eps_basic.value)),
            "face_value": None if lines.face_value.value is None else Decimal(str(lines.face_value.value))}


def neighbour_issues(s: Session, iid: int, period_end: date, revenue_cr: Decimal) -> tuple[list[str], Optional[dict]]:
    """Units check against stored quarters within a year, and the year-ago quarter for the YoY check."""
    rows = s.scalars(select(m.FundamentalSnapshot).where(
        m.FundamentalSnapshot.instrument_id == iid, m.FundamentalSnapshot.period_type == "Q")).all()
    near = [float(r.revenue) for r in rows if r.revenue and abs((r.period_end - period_end).days) <= 370 and r.period_end != period_end]
    issues = []
    if near and revenue_cr:
        med = median(near)
        ratio = float(revenue_cr) / med if med else None
        if ratio is not None and (ratio > NEIGHBOUR_RATIO or ratio < 1 / NEIGHBOUR_RATIO):
            issues.append(f"revenue ₹{float(revenue_cr):,.2f} cr is {ratio:.2g}x the nearby quarters' median ₹{med:,.2f} cr (units?)")
    ya = next((r for r in rows if r.period_end == period_end.replace(year=period_end.year - 1)), None)
    prior = None if ya is None else {"revenue": Decimal(str(ya.revenue)) * CRORE if ya.revenue is not None else None,
                                     "pat": Decimal(str(ya.pat)) * CRORE if ya.pat is not None else None}
    return issues, prior


async def extract_from_text(deps, f: sessionmaker[Session], symbol: str, period_end: date, text: str,
                            published: date, decision_time: datetime, *, nature: str = "consolidated",
                            source_ref: Optional[dict[str, Any]] = None, images: Optional[list[bytes]] = None,
                            ocr: bool = False) -> dict[str, Any]:
    from orchestrator.evidence import escape_text
    from orchestrator.schemas import StatementLines

    run_id = deps.store.create_run("filing_extraction", decision_time, f"llm15:{symbol}:{period_end}", deps.cfg.version,
                                   deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
    try:
        out = await deps.llm.structured(
            call_id="LLM-15", agent="filing_extractor", run_id=run_id, decision_time=decision_time,
            output_model=StatementLines, sections={"filing": f"<untrusted_document>{escape_text(text[:60000])}</untrusted_document>"},
            template_vars={"period_end": period_end.isoformat(), "nature": nature}, images=images)
    except Exception:
        deps.store.finish_run(run_id, "failed", deps.budget.run_spent)
        raise
    q = to_rupees(out, out.currency_unit)
    issues = ["unknown currency unit " + out.currency_unit] if q is None else check_statement(reconcile_owners(q))
    floor = MIN_CONFIDENCE_SCANNED if ocr else MIN_CONFIDENCE
    if out.extraction_confidence < floor:
        issues.append(f"extraction confidence {out.extraction_confidence:.2f} < {floor}")
    unread = [x for x in out.unreadable_fields if x in REQUIRED]
    if unread:
        issues.append("unreadable: " + ", ".join(unread))
    if out.period_end[:10] != period_end.isoformat():
        issues.append(f"extracted quarter {out.period_end} is not {period_end}")
    if out.nature != nature:
        issues.append(f"read a {out.nature} statement; this company's series is {nature}")
    with f() as s, s.begin():
        iid = upsert_instrument(s, symbol)
        if q is not None and q.get("revenue") is not None:
            more, prior = neighbour_issues(s, iid, period_end, (q["revenue"] / CRORE))
            issues += more
            if prior:
                issues += [i for i in check_statement(reconcile_owners(q), prior) if "YoY" in i]
        raw = out.model_dump(mode="json") | {"source_ref": source_ref or {}}
        if issues:
            dup = any((t.payload or {}).get("check") == "llm15_extraction" and t.payload.get("symbol") == symbol
                      and t.payload.get("period_end") == period_end.isoformat()
                      for t in s.scalars(select(m.HitlTask).where(m.HitlTask.task_type == "data_anomaly",
                                                                  m.HitlTask.status == "pending")))
            if not dup:
                s.add(m.HitlTask(task_type="data_anomaly", subject_type="instrument", subject_id=iid, priority=3,
                                 payload={"check": "llm15_extraction", "symbol": symbol, "period_end": period_end.isoformat(),
                                          "issues": issues, "extracted": raw},
                                 ai_recommendation="review", reason=f"{symbol} {period_end} PDF extraction: {'; '.join(issues)}"[:500],
                                 default_on_expiry="exclude_instrument"))
            status = "review"
        else:
            q = reconcile_owners(q)
            rev = (q["revenue"] / CRORE).quantize(Decimal("0.01"))
            pat = ((q.get("pat_owners") or q["pat"]) / CRORE).quantize(Decimal("0.01"))
            s.add(m.FundamentalSnapshot(instrument_id=iid, period_end=period_end, period_type="Q", published_at=published,
                                        revenue=rev, pat=pat, eps=q["eps_basic"], raw=raw,
                                        source=f"llm15_{'ocr_' if ocr else ''}{out.nature}"))
            status = "stored"
    deps.store.finish_run(run_id, "succeeded", deps.budget.run_spent)
    return {"status": status, "issues": issues, "run_id": run_id}
