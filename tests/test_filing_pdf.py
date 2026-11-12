"""Results-PDF fallback: picking the announcement, choosing statement pages, finding gaps, and the
LLM-15 checks (units vs neighbours, right quarter, right statement, required lines)."""
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core import models as m
from ingestion import filing_pdf as FP
from ingestion.adapters.nse_announcements import ResultPdf, parse_announcements, pick_result_pdfs, results_due
from ingestion.filing_text import extract_from_text
from orchestrator.schemas import StatementLines

from .synth import AS_OF, DECISION, IST, synthetic_db
from .test_phase4 import deps_for

PE = date(2025, 3, 31)


def ann(when, desc, text="", url="https://nsearchives.nseindia.com/corporate/X_Results.pdf"):
    return {"symbol": "ABC", "an_dt": when, "desc": desc, "attchmntText": text, "attchmntFile": url}


def test_results_due_and_pdf_ranking():
    assert results_due(PE) == date(2025, 5, 30) and results_due(date(2025, 6, 30)) == date(2025, 8, 14)
    rows = parse_announcements([
        ann("22-Apr-2025 17:49:54", "Outcome of Board Meeting", "Outcome of Board Meeting held on April 22, 2025.", "https://x/a.pdf"),
        ann("22-Apr-2025 18:30:00", "Financial Result Updates", "audited financial results", "https://x/b.pdf"),
        ann("25-Apr-2025 22:14:05", "Analysts/Institutional Investor Meet/Con. Call Updates", "Transcript", "https://x/c.pdf"),
        ann("22-Apr-2025 17:53:29", "Press Release", "Investor Release Q4 FY 2025", "https://x/d.pdf"),
        ann("10-Mar-2025 10:00:00", "Financial Result Updates", "old quarter", "https://x/e.pdf"),
        ann("23-Apr-2025 10:00:00", "Financial Result Updates", "zip", "https://x/f.zip"),
        ann("24-Apr-2025 10:00:00", "Financial Result Updates", "", "https://nsearchives.nseindia.com/corporate/null"),
    ])
    docs = pick_result_pdfs(rows, "ABC", PE)
    assert [d.url[-5:] for d in docs] == ["b.pdf", "a.pdf"] and docs[0].score == 3 and docs[1].published_at.hour == 17


STATEMENT = ("Consolidated Statement of Financial Results\nParticulars Three months ended 31 March 2025\n"
             "Revenue from operations 30,246\nOther income 449\nTotal income 30,695\nProfit before tax 5,735\n"
             "Tax expense 1,426\nProfit for the period 4,309\n")
STANDALONE = STATEMENT.replace("Consolidated", "Standalone")


def test_select_pages_prefers_wanted_statement_and_joins_eps_page():
    pages = ["Cover letter to the exchanges", STANDALONE + "Earnings per share 9.1", STATEMENT,
             "Earnings per equity share Basic 15.90\nPaid-up equity share capital 543", "Independent Auditor's Report"]
    text, used, nature = FP.select_pages(pages, "consolidated")
    assert used == [3, 4] and nature == "consolidated" and "--- page 3 ---" in text and "15.90" in text
    _, used, nature = FP.select_pages(pages, "standalone")
    assert used == [2] and nature == "standalone"
    assert FP.select_pages(["notes only", "auditor"], "consolidated") == ("", [], "none")
    assert FP.is_scanned(["", "", "", " "]) and not FP.is_scanned(pages)


def test_find_gaps_respects_due_dates_listing_and_insurers():
    _, f = synthetic_db(n_up=2, n_flat=0, n_down=0, days=600)
    today = date(2026, 10, 14)
    with f() as s, s.begin():
        up0, up1 = (s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == x)) for x in ("UP0", "UP1"))
        for pe in FP.quarter_ends(today, 8):
            if pe != PE:
                s.add(m.FundamentalSnapshot(instrument_id=up0, period_end=pe, period_type="Q", published_at=pe + timedelta(days=20),
                                            revenue=Decimal(100), pat=Decimal(10), source="nse_xbrl_standalone"))
        s.get(m.Instrument, up1).industry = "Insurance"
    with f() as s:
        gaps = FP.find_gaps(s, today, 8)
    assert [(g.symbol, g.period_end, g.nature) for g in gaps] == [("UP0", PE, "standalone")]
    with f() as s:                                         # before the due date the quarter isn't missing yet
        assert all(g.period_end != PE for g in FP.find_gaps(s, date(2025, 5, 20), 8, symbols=["UP0"]))


class StubLLM:
    """Returns StatementLines built from overrides, for exercising the checks."""

    def __init__(self, store, **over):
        self.store, self.over, self.calls = store, over, []

    async def structured(self, *, call_id, template_vars, **kw):
        self.calls.append(template_vars)
        line = lambda v: {"value": v, "page": 3}
        base = {"period_end": template_vars["period_end"], "nature": template_vars["nature"], "currency_unit": "INR_crore",
                "revenue": line(1000.0), "other_income": line(50.0), "total_income": line(1050.0),
                "profit_before_tax": line(200.0), "tax_expense": line(50.0), "net_profit": line(150.0),
                "net_profit_owners": line(150.0), "eps_basic": line(15.0), "paid_up_capital": line(100.0),
                "face_value": line(10.0), "extraction_confidence": 0.93, "unreadable_fields": []}
        return StatementLines.model_validate(base | self.over)


def run_extract(f, want="consolidated", **over):
    deps = deps_for(f)
    deps.llm = StubLLM(deps.store, **over)
    out = asyncio.run(extract_from_text(deps, f, "UP0", PE, "statement text", date(2025, 4, 22), DECISION, nature=want,
                                        source_ref={"url": "https://x/b.pdf", "pages": [3]}))
    return out, deps.llm


def seed_neighbours(f, revenue=1000):
    with f() as s, s.begin():
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == "UP0"))
        for pe in (date(2024, 12, 31), date(2025, 6, 30), date(2024, 3, 31)):
            s.add(m.FundamentalSnapshot(instrument_id=iid, period_end=pe, period_type="Q", published_at=pe + timedelta(days=30),
                                        revenue=Decimal(revenue), pat=Decimal(150), source="nse_xbrl_consolidated"))


def test_extraction_stores_with_source_and_tolerates_optional_gaps():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    seed_neighbours(f)
    out, llm = run_extract(f, paid_up_capital={"value": None, "page": None}, face_value={"value": None, "page": None},
                           unreadable_fields=["paid_up_capital", "face_value"])
    assert out["status"] == "stored", out["issues"]
    assert llm.calls[0] == {"period_end": "2025-03-31", "nature": "consolidated"}
    with f() as s:
        snap = s.scalar(select(m.FundamentalSnapshot).where(m.FundamentalSnapshot.period_end == PE))
        assert snap.source == "llm15_consolidated" and snap.revenue == Decimal("1000.00") and snap.published_at == date(2025, 4, 22)
        assert snap.raw["source_ref"]["url"] == "https://x/b.pdf"


@pytest.mark.parametrize("over,needle", [
    ({"currency_unit": "INR_lakh"}, "units?"),                                  # 1000 lakh = 10 cr vs ~1000 cr neighbours
    ({"nature": "standalone"}, "standalone statement"),
    ({"period_end": "2024-03-31"}, "is not 2025-03-31"),
    ({"unreadable_fields": ["net_profit"]}, "unreadable: net_profit"),
    ({"extraction_confidence": 0.6}, "confidence"),
    ({"net_profit": {"value": 90.0, "page": 3}, "net_profit_owners": {"value": 90.0, "page": 3}}, "PBT - tax"),
])
def test_extraction_checks_route_to_review(over, needle):
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    seed_neighbours(f)
    out, _ = run_extract(f, **over)
    assert out["status"] == "review" and any(needle in i for i in out["issues"]), out["issues"]
    run_extract(f, **over)                                                    # a second run doesn't duplicate the task
    with f() as s:
        assert s.scalar(select(func.count()).select_from(m.HitlTask).where(m.HitlTask.task_type == "data_anomaly")) == 1
        assert s.scalar(select(func.count()).select_from(m.FundamentalSnapshot).where(m.FundamentalSnapshot.period_end == PE)) == 0


class FakeAnn:
    def __init__(self, docs, pdf=b"%PDF-1.7 test"):
        self.docs, self.data, self.fetched = docs, pdf, []

    def result_pdfs(self, symbol, period_end):
        return self.docs

    def pdf(self, doc):
        self.fetched.append(doc.url)
        return self.data


def test_fill_gap_end_to_end_and_fallbacks(monkeypatch):
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    iid = 1
    gap = FP.Gap("UP0", iid, PE, "consolidated")
    doc = ResultPdf("UP0", PE, "https://x/b.pdf", datetime(2025, 4, 22, 18, 30), "Financial Result Updates", "", 3)
    scanned = ResultPdf("UP0", PE, "https://x/scan.pdf", datetime(2025, 4, 22, 17, 0), "Outcome of Board Meeting", "", 2)
    pages = {"https://x/scan.pdf": ["", "", ""], "https://x/b.pdf": ["cover letter " * 40, STATEMENT + "Earnings per share 15"]}
    src = FakeAnn([scanned, doc])
    monkeypatch.setattr(FP, "pdf_pages", lambda data: pages[src.fetched[-1]])
    deps = deps_for(f)
    deps.llm = StubLLM(deps.store)
    dry = asyncio.run(FP.fill_gap(deps, f, src, gap, DECISION, dry_run=True))
    assert dry["status"] == "dry_run" and dry["url"] == "https://x/b.pdf" and dry["pages"] == [2] and deps.llm.calls == []
    out = asyncio.run(FP.fill_gap(deps, f, src, gap, DECISION))
    assert out["status"] == "stored" and out["pages"] == [2]
    assert asyncio.run(FP.fill_gap(deps, f, FakeAnn([]), gap, DECISION)) == {"status": "no_pdf"}
    src2 = FakeAnn([scanned])
    monkeypatch.setattr(FP, "pdf_pages", lambda data: ["", "", ""])
    assert asyncio.run(FP.fill_gap(deps, f, src2, gap, DECISION))["status"] == "scanned"


def test_fill_gap_skips_when_only_the_other_statement_is_readable(monkeypatch):
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    doc = ResultPdf("UP0", PE, "https://x/b.pdf", datetime(2025, 4, 22, 18, 30), "Financial Result Updates", "", 3)
    monkeypatch.setattr(FP, "pdf_pages", lambda data: ["cover letter " * 40, STANDALONE + "Earnings per share 15"])
    deps = deps_for(f)
    deps.llm = StubLLM(deps.store)
    out = asyncio.run(FP.fill_gap(deps, f, FakeAnn([doc]), FP.Gap("UP0", 1, PE, "consolidated"), DECISION))
    assert out["status"] == "only_standalone_readable" and deps.llm.calls == []


def test_results_pdf_job_fills_gaps_with_scripted_llm(monkeypatch):
    from core.settings import Settings
    from scheduler import jobs

    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=700)
    doc = lambda sym, pe: [ResultPdf(sym, pe, f"https://x/{pe}.pdf", datetime.combine(pe + timedelta(days=30), datetime.min.time()),
                                     "Financial Result Updates", "", 3)]
    src = FakeAnn([])
    src.result_pdfs = doc
    monkeypatch.setattr(FP, "pdf_pages", lambda data: ["cover letter " * 40, STATEMENT + "Earnings per share 15"])
    rt = jobs.Runtime(deps=deps_for(f), f=f, settings=Settings(universe="NIFTY200"), adapters=lambda: None,
                      now=lambda: datetime(2026, 10, 14, 13, 0, tzinfo=IST))
    rt.pdf_source = src
    out = asyncio.run(jobs.results_pdf_job(rt, limit=3))
    assert out["tried"] == 3 and out["stored"] == 3 and not out["errors"], out
    with f() as s:
        assert s.scalar(select(func.count()).select_from(m.FundamentalSnapshot).where(m.FundamentalSnapshot.source == "llm15_consolidated")) == 3
