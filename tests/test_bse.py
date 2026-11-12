"""BSE fallback for BSE-only filers (e.g. MCX): scrip lookup, results PDFs, shareholding summary,
and the NSE -> BSE chaining used by the PDF pipeline and the shareholding ingest."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from core import models as m
from ingestion.adapters.base import DiskCache
from ingestion.adapters.bse import Bse, parse_public_categories, parse_results, parse_search, parse_shp_summary, qtrid
from ingestion.adapters.nse_announcements import ResultPdf
from ingestion.filing_pdf import ChainedResults
from ingestion.shareholding import ShareholdingReport, ingest_symbol

from .synth import IST, synthetic_db

SEARCH = ("<li class='quotemenu' ng-click=\"liclick('534091','MULTI COMMODITY EXCHANGE OF INDIA LTD')\"><a>MULTI COMMODITY EXCHANGE"
          " OF INDIA LTD<br /><span><strong>MCX</strong>&nbsp;&nbsp;&nbsp;INE745G01043&nbsp;&nbsp;&nbsp;534091</span></a></li>")
SUMMARY = ("<table><tr><td>Quarter ending :June 2025</td></tr><tr><td>Whether any shares held by promoters are pledge or otherwise"
           " encumbered?</td><td>No</td></tr><tr><td>(A) Promoter &amp; Promoter Group</td><td>0.00</td><td>0.00</td><td>0.00</td>"
           "<td>--</td></tr><tr><td>(B) Public</td><td>2,34,988</td><td>5,09,02,818</td><td>5,09,02,818</td><td>99.81</td></tr>"
           "<tr><td>(C2) Shares held by Employee Trust</td><td>1</td><td>95,551</td><td>95,551</td><td>0.19</td></tr>"
           "<tr><td>Grand Total</td><td>2,34,989</td><td>5,09,98,369</td><td>5,09,98,369</td><td>100.00</td></tr></table>")


def test_parsers():
    assert parse_search(SEARCH) == [{"code": "534091", "name": "MULTI COMMODITY EXCHANGE OF INDIA LTD", "symbol": "MCX",
                                     "isin": "INE745G01043"}]
    assert qtrid(date(2026, 6, 30)) == 130 and qtrid(date(2025, 12, 31)) == 128 and qtrid(date(2024, 6, 30)) == 122
    docs = parse_results({"Table": [
        {"NEWS_DT": "2025-05-08T19:48:48.697", "SUBCATNAME": "Financial Results", "ATTACHMENTNAME": "a.pdf", "NEWSSUB": "FR"},
        {"NEWS_DT": "2025-05-09T10:00:00", "SUBCATNAME": "Investor Presentation", "ATTACHMENTNAME": "b.pdf"},
        {"NEWS_DT": "2025-05-09T10:00:00", "SUBCATNAME": "Financial Results", "ATTACHMENTNAME": "c.zip"}]}, "MCX", date(2025, 3, 31))
    assert [d.url.rsplit("/", 1)[-1] for d in docs] == ["a.pdf"] and docs[0].published_at == datetime(2025, 5, 8, 19, 48, 48)
    sh = parse_shp_summary(SUMMARY)
    assert sh["quarter"] == "June 2025" and sh["promoter_pct"] == 0 and sh["no_promoter_group"]
    assert sh["public_pct"] == Decimal("99.81") and sh["trust_pct"] == Decimal("0.19") and sh["shareholders"] == 234989
    assert sh["promoter_encumbered_pct"] == 0


def row(sub, level, pct, name=None):
    return {"Fld_SubCategory": sub, "Fld_Level": level, "Fld_TotalPercentageOf_A_B_C2": pct, "Fld_ShareHolderName": name}


CATEGORIES = {"Table1": [
    row("Institutions", "Mutual Funds/", 30.07), row("Institutions", "Mutual Funds/", 4.48, "SOME FUND"),
    row("Institutions", "Insurance  Companies", 4.77), row("Institutions (Domestic)", "Sub Total B1", 50.81),
    row("Institutions (Foreign)", "Sub Total B2", 29.84),
    row("Central  Government/  State  Government(s)/ President of India", "Sub Total B3", 0.01),
    row("Non-Institutions", "Resident Individuals holding nominal share capital up to Rs. 2 lakhs", 15.19),
    row("Non-Institutions", "Resident Individuals holding nominal share capital in excess of Rs. 2 lakhs", 0.64),
    row("", "B=B1+B2+B3+B4", 99.81)]}


def test_public_categories_parser():
    c = parse_public_categories(CATEGORIES)
    assert c == {"dii_pct": Decimal("50.81"), "fpi_pct": Decimal("29.84"), "mf_pct": Decimal("30.07"),
                 "insurance_pct": Decimal("4.77"), "government_pct": Decimal("0.01"), "retail_pct": Decimal("15.83"),
                 "public_total_pct": Decimal("99.81")}                       # named holders ignored
    odd = parse_public_categories({"Table1": [row("Institutions", "Sub Total B1", 80.0), row("", "B=B1+B2+B3+B4", 99.0)]})
    assert odd["dii_pct"] is None and odd["fpi_pct"] is None                  # unfamiliar layout: missing, not wrong
    assert parse_public_categories({"Table1": []})["public_total_pct"] is None


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, path, params):
        self.calls.append(path)
        if path.startswith("PeerSmartSearch"):
            return SEARCH
        if path.startswith("AnnSubCategoryGetData"):
            return {"Table": [{"NEWS_DT": "2025-08-01T18:00:00", "SUBCATNAME": "Financial Results", "ATTACHMENTNAME": "q1.pdf", "NEWSSUB": "FR"}]}
        if path.startswith("shpSecSummery_New"):
            return {"Data": SUMMARY}
        if path.startswith("Corp_shpSec_SHPPubShold_ng"):
            return CATEGORIES
        raise AssertionError(path)

    def get_bytes(self, url):
        self.calls.append(url)
        if "AttachLive" in url:
            from ingestion.adapters.base import PermanentError
            raise PermanentError("moved")
        return b"%PDF-1.7 bse"


def test_bse_adapter_with_caching(tmp_path):
    http = FakeHttp()
    b = Bse(DiskCache(tmp_path), get=http.get, get_bytes=http.get_bytes, today=lambda: date(2026, 10, 14))
    assert b.scrip_code("MCX", "INE745G01043") == "534091" and b.scrip_code("MCX", "INE745G01043") == "534091"
    docs = b.result_pdfs("MCX", date(2025, 6, 30), "INE745G01043")
    assert len(docs) == 1 and b.pdf(docs[0]) == b"%PDF-1.7 bse"                 # falls back from AttachLive to AttachHis
    assert any("AttachHis" in c for c in http.calls)
    shp = b.shareholding("MCX", date(2025, 6, 30), "INE745G01043")
    assert shp.summary["public_pct"] == Decimal("99.81") and shp.published_at == datetime(2025, 7, 21, 23, 59)
    assert b.shareholding("MCX", date(2025, 9, 30), "INE745G01043") is None      # BSE answered with another quarter
    n = len(http.calls)
    b2 = Bse(DiskCache(tmp_path), get=http.get, get_bytes=http.get_bytes, today=lambda: date(2026, 10, 14))
    b2.scrip_code("MCX", "INE745G01043"), b2.result_pdfs("MCX", date(2025, 6, 30), "INE745G01043"), b2.pdf(docs[0])
    b2.shareholding("MCX", date(2025, 6, 30), "INE745G01043")
    assert len(http.calls) == n                                                    # everything settled came from the cache


class Src:
    def __init__(self, docs):
        self.docs, self.got = docs, []

    def result_pdfs(self, symbol, period_end, *isin):
        return self.docs

    def pdf(self, doc):
        self.got.append(doc.url)
        return b"%PDF"


def test_chained_results_prefers_nse():
    nse_doc = ResultPdf("X", date(2025, 3, 31), "https://nsearchives.nseindia.com/a.pdf", datetime(2025, 5, 1), "", "", 3)
    bse_doc = ResultPdf("X", date(2025, 3, 31), "https://www.bseindia.com/xml-data/corpfiling/AttachLive/b.pdf", datetime(2025, 5, 1), "", "", 3)
    nse, bse = Src([nse_doc]), Src([bse_doc])
    assert ChainedResults(nse, bse).result_pdfs("X", date(2025, 3, 31)) == [nse_doc]
    nse.docs = []
    chain = ChainedResults(nse, bse)
    (d,) = chain.result_pdfs("X", date(2025, 3, 31))
    chain.pdf(d)
    assert bse.got == [bse_doc.url] and nse.got == []


class NoNse:
    def filings(self, symbol):
        return []


def test_shareholding_falls_back_to_bse(tmp_path):
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    with f() as s, s.begin():
        s.scalar(select(m.Instrument).where(m.Instrument.symbol == "UP0")).isin = "INE745G01043"   # matched on ISIN
    http = FakeHttp()                       # BSE always answers with the June 2025 summary
    b = Bse(DiskCache(tmp_path), get=http.get, get_bytes=http.get_bytes)
    rep = ingest_symbol(f, NoNse(), "UP0", quarters=2, rep=ShareholdingReport(), bse=b, today=date(2025, 8, 20))
    assert rep.stored == 1 and rep.errors == ["UP0 2025-03-31: no BSE shareholding summary"]   # wrong quarter is refused
    with f() as s:
        row = s.scalar(select(m.Shareholding))
        assert row.period_end == date(2025, 6, 30) and row.source == "bse_shp" and row.promoter_pct == 0
        assert row.fpi_pct == Decimal("29.84") and row.dii_pct == Decimal("50.81") and row.raw["source"] == "bse_summary+categories"
        assert row.raw["published_at_estimated"] and row.published_at.astimezone(IST).date() == date(2025, 7, 21)
    again = ingest_symbol(f, NoNse(), "UP0", quarters=2, rep=ShareholdingReport(), bse=b, today=date(2025, 8, 20))
    assert again.skipped_existing == 1
    early = ingest_symbol(f, NoNse(), "UP0", quarters=1, rep=ShareholdingReport(), bse=b, today=date(2025, 7, 10))
    assert early.stored == 0 and not early.errors                                 # June not due until 21 July


def test_categories_fill_old_rows_and_mismatch_is_dropped(tmp_path):
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    with f() as s, s.begin():
        inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == "UP0"))
        inst.isin = "INE745G01043"
        s.add(m.Shareholding(instrument_id=inst.id, period_end=date(2025, 6, 30), published_at=datetime(2025, 7, 21, 23, 59, tzinfo=IST),
                             promoter_pct=Decimal(0), public_pct=Decimal("99.81"), raw={"source": "bse_summary"}, source="bse_shp"))
    http = FakeHttp()
    b = Bse(DiskCache(tmp_path), get=http.get, get_bytes=http.get_bytes)
    rep = ingest_symbol(f, NoNse(), "UP0", quarters=1, rep=ShareholdingReport(), bse=b, today=date(2025, 8, 20))
    assert rep.updated == 1 and rep.stored == 0
    with f() as s:
        r = s.scalar(select(m.Shareholding))
        assert r.fpi_pct == Decimal("29.84") and r.raw["source"] == "bse_summary+categories"
    bad = dict(CATEGORIES, Table1=[x if x["Fld_Level"] != "B=B1+B2+B3+B4" else {**x, "Fld_TotalPercentageOf_A_B_C2": 90.0}
                                   for x in CATEGORIES["Table1"]])
    http2 = FakeHttp()
    http2.get = lambda path, params: bad if path.startswith("Corp_shpSec_SHPPubShold_ng") else FakeHttp().get(path, params)
    _, f2 = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    with f2() as s, s.begin():
        s.scalar(select(m.Instrument).where(m.Instrument.symbol == "UP0")).isin = "INE745G01043"
    b2 = Bse(DiskCache(tmp_path / "b2"), get=http2.get, get_bytes=http2.get_bytes)
    rep2 = ingest_symbol(f2, NoNse(), "UP0", quarters=1, rep=ShareholdingReport(), bse=b2, today=date(2025, 8, 20))
    assert rep2.stored == 1 and "categories dropped" in rep2.issues[0]
    with f2() as s:
        assert s.scalar(select(m.Shareholding.fpi_pct)) is None
