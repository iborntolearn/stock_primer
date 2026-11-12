"""OCR path for scanned results PDFs: heading screening, page choice, images to the model, caching,
and the stricter confidence floor for values read from scans."""
import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core import models as m
from ingestion import filing_pdf as FP
from ingestion import ocr as O
from ingestion.adapters.base import DiskCache
from ingestion.adapters.nse_announcements import ResultPdf
from orchestrator.llm import ClaudeLLM, render_request
from orchestrator.budget import BudgetManager
from orchestrator.config import Config
from orchestrator.schemas import RouteDecision

from .synth import DECISION, synthetic_db
from .test_filing_pdf import PE, STATEMENT, FakeAnn, StubLLM
from .test_phase4 import deps_for
from .test_units import GOOD_ROUTE, client as stub_client, resp

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


def test_lines_from_orders_boxes_into_lines():
    box = lambda x, y, t: ([[x, y], [x + 50, y], [x + 50, y + 10], [x, y + 10]], t, 0.9)
    res = [box(200, 101, "30,246"), box(10, 100, "Revenue from operations"), box(10, 130, "Other income"), box(200, 129, "449")]
    assert O.lines_from(res) == "Revenue from operations 30,246\nOther income 449"
    assert O.lines_from(None) == ""


@pytest.mark.parametrize("text,ok,nature", [
    ("POWER FINANCE CORPORATION LTD\nStatementofAuditedStandaloneFinancialResultsforthequarterandyearended31.03.2025", True, "standalone"),
    ("Consolidated Financial Results for the Quarter ended 31 March 2025", True, "consolidated"),
    ("Notes to the Consolidated Financial Results: 1. Consolidated Statement of Assets and Liabilities", False, "consolidated"),
    ("Independent Auditor's Report on the Statement of Standalone Financial Results", False, "standalone"),
    ("It is to further mention that dividend income is taxable", False, "unclear"),
    ("Annexure A Multi Commodity Exchange of India Limited Statement of Audited Consolidated Financial Results for the quarter and year ended", True, "consolidated"),
    ("Annexure-A Disclosure as per the Regulation 52(4) of SEBI for the quarter ended (financial results)", False, "unclear"),
])
def test_statement_heading(text, ok, nature):
    assert FP.statement_heading(text) == (ok, nature)


def test_ocr_candidates_order():
    pages = ["", "cover letter " * 30, "garbled scan text layer ffiIffi " * 20, STATEMENT, "  "]
    assert FP.ocr_candidates(pages, 5) == [0, 4, 1, 2]                 # blank pages first, then murky; not the clean statement
    assert FP.ocr_candidates([], 3) == [0, 1, 2]


class FakeOcr:
    def __init__(self, heads, fulls):
        self.heads, self.fulls, self.calls = heads, fulls, []

    def heading(self, doc, h, i):
        self.calls.append(("head", i))
        return self.heads.get(i, "")

    def full(self, doc, h, i):
        self.calls.append(("full", i))
        return self.fulls.get(i, "")

    def png(self, doc, i):
        return PNG + bytes([i])


CONS_HEAD = "Statement of Audited Consolidated Financial Results for the quarter ended 31.03.2025"
STAND_HEAD = CONS_HEAD.replace("Consolidated", "Standalone")


def scanned_pdf(monkeypatch, n=8):
    monkeypatch.setattr(O, "open_pdf", lambda data: (list(range(n)), "hash"))


def test_find_scanned_statement_stops_at_the_wanted_page(monkeypatch):
    scanned_pdf(monkeypatch)
    ocr = FakeOcr({2: STAND_HEAD, 4: CONS_HEAD, 6: CONS_HEAD},
                  {2: STATEMENT.replace("Consolidated", "Standalone"), 4: STATEMENT, 5: "Earnings per equity share Basic 15.90"})
    hit = FP.find_scanned_statement(b"%PDF", [""] * 8, "consolidated", ocr)
    assert hit["pages"] == [5, 6] and hit["nature"] == "consolidated" and len(hit["images"]) == 2
    assert "OCR transcript" in hit["text"] and "15.90" in hit["text"]
    assert ("head", 6) not in ocr.calls and ("full", 2) not in ocr.calls      # stopped early; standalone never fully OCR'd
    assert FP.find_scanned_statement(b"%PDF", [""] * 8, "consolidated", FakeOcr({}, {})) is None


def test_ocr_cache(tmp_path):
    engine_calls = []

    class Engine:
        def __call__(self, img, use_cls=False):
            engine_calls.append(img.size)
            return [([[0, 0], [10, 0], [10, 10], [0, 10]], "Statement of Financial Results", 0.9)], 0.1

    class Page:
        def render(self, scale):
            from PIL import Image
            return SimpleNamespace(to_pil=lambda: Image.new("RGB", (int(100 * scale), int(140 * scale)), "white"))

    o = O.Ocr(DiskCache(tmp_path), engine=Engine())
    doc = [Page()]
    assert o.heading(doc, "h1", 0) == o.heading(doc, "h1", 0) == "Statement of Financial Results"
    assert len(engine_calls) == 1 and engine_calls[0] == (140, int(196 * O.HEAD_BAND))
    assert O.Ocr(DiskCache(tmp_path), engine=Engine()).heading(doc, "h1", 0) and len(engine_calls) == 1   # from disk
    img = o.png(doc, 0)
    assert img[:8] == b"\x89PNG\r\n\x1a\n"


def test_fill_gap_uses_ocr_and_sends_images(monkeypatch):
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    scanned_pdf(monkeypatch)
    monkeypatch.setattr(FP, "pdf_pages", lambda data: [""] * 8)
    ocr = FakeOcr({4: CONS_HEAD}, {4: STATEMENT + "Earnings per share 15"})
    doc = ResultPdf("UP0", PE, "https://x/scan.pdf", datetime(2025, 5, 20, 18), "Outcome of Board Meeting", "", 2)
    deps = deps_for(f)
    llm = deps.llm = StubLLM(deps.store)
    seen = {}
    orig = llm.structured

    async def spy(**kw):
        seen["images"] = kw.get("images")
        return await orig(**kw)
    llm.structured = spy
    gap = FP.Gap("UP0", 1, PE, "consolidated")
    dry = asyncio.run(FP.fill_gap(deps, f, FakeAnn([doc]), gap, DECISION, dry_run=True, ocr=ocr))
    assert dry["status"] == "dry_run" and dry["ocr"] and dry["pages"] == [5]
    out = asyncio.run(FP.fill_gap(deps, f, FakeAnn([doc]), gap, DECISION, ocr=ocr))
    assert out["status"] == "stored" and seen["images"] == [PNG + bytes([4])]
    with f() as s:
        snap = s.scalar(select(m.FundamentalSnapshot))
        assert snap.source == "llm15_ocr_consolidated" and snap.raw["source_ref"]["ocr"] is True
    nothing = asyncio.run(FP.fill_gap(deps, f, FakeAnn([doc]), gap, DECISION, ocr=FakeOcr({}, {})))
    assert nothing["status"] == "scanned+ocr_not_found"


def test_scanned_extractions_need_higher_confidence(monkeypatch):
    from ingestion.filing_text import extract_from_text

    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    deps = deps_for(f)
    deps.llm = StubLLM(deps.store, extraction_confidence=0.83)
    text_ok = asyncio.run(extract_from_text(deps, f, "UP0", PE, "t", date(2025, 5, 20), DECISION))
    assert text_ok["status"] == "stored"
    _, f2 = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    deps2 = deps_for(f2)
    deps2.llm = StubLLM(deps2.store, extraction_confidence=0.83)
    scan = asyncio.run(extract_from_text(deps2, f2, "UP0", PE, "t", date(2025, 5, 20), DECISION, images=[PNG], ocr=True))
    assert scan["status"] == "review" and any("0.85" in i for i in scan["issues"])


def test_claude_request_carries_images_and_hash_changes():
    cfg = Config()
    h1 = render_request(cfg, "LLM-01", DECISION, {"message": "x"}, None)[3]
    h2 = render_request(cfg, "LLM-01", DECISION, {"message": "x"}, None, [PNG])[3]
    assert h1 != h2
    c, stub = stub_client([resp(GOOD_ROUTE)])
    from orchestrator.fakes import InMemoryStore
    llm = ClaudeLLM(cfg, InMemoryStore(), BudgetManager(5, 5), client=c)
    asyncio.run(llm.structured(call_id="LLM-01", agent="router", run_id=1, decision_time=DECISION,
                               sections={"message": "INFY?", "session_context": ""}, output_model=RouteDecision, images=[PNG]))
    content = stub.requests[0]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text"]
    assert content[1]["source"]["media_type"] == "image/png"


@pytest.mark.skipif(not O.available(), reason="OCR packages not installed")
def test_real_ocr_reads_rendered_text():
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 220), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=36)
    d.text((20, 30), "Statement of Consolidated Financial Results", fill="black", font=font)
    d.text((20, 120), "Revenue from operations 30,246", fill="black", font=font)
    res, _ = O.Ocr().engine(img, use_cls=False)
    text = O.lines_from(res)
    assert "30246" in text.replace(",", "").replace(" ", "")
    assert FP.statement_heading(text) == (True, "consolidated")


def test_text_pdfs_also_send_page_images(monkeypatch):
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    scanned_pdf(monkeypatch)
    monkeypatch.setattr(FP, "pdf_pages", lambda data: ["cover letter " * 40, STATEMENT + "Earnings per share 15"])
    doc = ResultPdf("UP0", PE, "https://x/b.pdf", datetime(2025, 5, 20, 18), "Financial Result Updates", "", 3)
    deps = deps_for(f)
    llm = deps.llm = StubLLM(deps.store)
    seen = {}
    orig = llm.structured

    async def spy(**kw):
        seen["images"] = kw.get("images")
        return await orig(**kw)
    llm.structured = spy
    out = asyncio.run(FP.fill_gap(deps, f, FakeAnn([doc]), FP.Gap("UP0", 1, PE, "consolidated"), DECISION, ocr=FakeOcr({}, {})))
    assert out["status"] == "stored" and out["ocr"] is False and out["images"] == 1 and seen["images"] == [PNG + bytes([1])]
    with f() as s:
        assert s.scalar(select(m.FundamentalSnapshot.source)) == "llm15_consolidated"
