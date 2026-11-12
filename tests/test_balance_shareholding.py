"""Balance sheets (XBRL instant context) -> ROE/ROCE/debt-equity/P/B, and shareholding patterns
(both XBRL formats) -> point-in-time `shareholding` rows and the get_shareholding tool."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core import models as m
from ingestion.adapters.nse_results import balance_sheet, parse_xbrl
from ingestion.adapters.nse_shareholding import ShpFiling, parse_master, parse_shp
from ingestion.fundamentals import backfill_balance_sheets, ingest_symbol
from ingestion.shareholding import check_pattern, ingest_symbol as ingest_sh
from quant.fundamentals import balance_ratios, metrics, sector_medians

from .synth import IST, synthetic_db
from .test_phase4 import CR, FakeResults, filing, xbrl
from .test_tools import call, make_service


def with_bs(xml: str, **bs) -> str:
    tags = "".join(f'<in-capmkt:{k} contextRef="OneI" unitRef="INR" decimals="-5">{v}</in-capmkt:{k}>' for k, v in bs.items())
    return xml.replace("</xbrli:xbrl>", tags + "</xbrli:xbrl>")


COMPANY_BS = dict(EquityAttributableToOwnersOfParent=900 * CR, Equity=1000 * CR, BorrowingsNoncurrent=300 * CR,
                  BorrowingsCurrent=200 * CR, Assets=2500 * CR, CashAndCashEquivalents=150 * CR)


# -- balance sheet parsing ----------------------------------------------------------------------------------
def test_balance_sheet_kinds():
    co = balance_sheet(parse_xbrl(with_bs(xbrl("2025-09-30"), **COMPANY_BS)))
    assert co["kind"] == "company" and co["equity_owners"] == 900 * CR and co["debt"] == 500 * CR and co["cash"] == 150 * CR
    nbfc = balance_sheet(parse_xbrl(with_bs(xbrl("2025-09-30"), Loans=4000 * CR, DebtSecurities=1000 * CR, Borrowings=2000 * CR,
                                            Deposits=500 * CR, EquityAttributableToOwnersOfParent=900 * CR, Equity=900 * CR, Assets=5000 * CR)))
    assert nbfc["kind"] == "nbfc" and nbfc["debt"] == 3500 * CR
    bank = balance_sheet(parse_xbrl(with_bs(xbrl("2025-09-30", bank=True), Capital=100 * CR, ReservesAndSurplus=900 * CR,
                                            Deposits=8000 * CR, Borrowings=600 * CR, Advances=7000 * CR, Assets=10000 * CR)))
    assert bank["kind"] == "bank" and bank["equity_owners"] == 1000 * CR and bank["debt"] == 600 * CR
    assert balance_sheet(parse_xbrl(xbrl("2025-06-30"))) is None                       # quarters without a balance sheet
    neg = balance_sheet(parse_xbrl(with_bs(xbrl("2025-09-30"), EquityAttributableToOwnersOfParent=-50 * CR, Equity=-50 * CR, Assets=900 * CR)))
    assert neg["equity_owners"] == -50 * CR and not neg.get("invalid")                 # accumulated losses are real
    bad = balance_sheet(parse_xbrl(with_bs(xbrl("2025-09-30"), EquityAttributableToOwnersOfParent=900 * CR, Equity=5000 * CR, Assets=2500 * CR)))
    assert bad["invalid"]


# -- ratios ---------------------------------------------------------------------------------------------------
def snap(pe, pat, equity=None, debt=None, **raw):
    return SimpleNamespace(period_end=pe, published_at=pe + timedelta(days=30), revenue=Decimal(1000), pat=Decimal(pat), eps=Decimal(10),
                           op_margin=None, equity=None if equity is None else Decimal(equity), total_debt=None if debt is None else Decimal(debt),
                           source="nse_xbrl_consolidated", raw={k: str(v) for k, v in raw.items()})


def quarters(kind="company", eq_now=1000, eq_then=800):
    """Newest first: four quarters of 50 cr profit; balance sheets at Sep-25 and Sep-24."""
    common = {"pbt": 70 * CR, "finance_costs": 10 * CR}
    return [snap(date(2025, 12, 31), 50, **common),
            snap(date(2025, 9, 30), 50, eq_now, 500, bs_kind=kind, bs_equity_total=1100 * CR, bs_cash=150 * CR,
                 bs_assets=4000 * CR, paid_up_capital=100 * CR, face_value=10, **common),
            snap(date(2025, 6, 30), 50, **common), snap(date(2025, 3, 31), 50, **common),
            snap(date(2024, 12, 31), 40, **common),
            snap(date(2024, 9, 30), 40, eq_then, 400, bs_kind=kind, bs_equity_total=900 * CR, **common)]


def test_company_ratios():
    r = balance_ratios(quarters(), price=200.0)
    assert r["balance_sheet_as_of"] == "2025-09-30" and r["averaged_with_year_ago"]
    assert r["roe_pct"] == pytest.approx(200 / 900 * 100, abs=0.01)                 # TTM 200 / avg(1000, 800)
    assert r["roce_pct"] == pytest.approx(320 / ((1100 + 500 + 900 + 400) / 2) * 100, abs=0.01)   # TTM EBIT (70+10)x4
    assert r["debt_equity"] == pytest.approx(500 / 1100, abs=0.01) and r["net_debt_cr"] == 350
    assert r["pb"] == pytest.approx(200 * 1e8 / (1000 * 1e7), abs=0.01)                 # 10 cr shares at 200 vs 1000 cr book


def test_lender_and_negative_equity_ratios():
    b = balance_ratios(quarters(kind="bank"), price=None)
    assert "debt_equity" not in b and "roce_pct" not in b and b["assets_to_equity"] == pytest.approx(4000 / 1100, abs=0.01)
    n = balance_ratios(quarters(eq_now=-300), price=100.0)
    assert n["negative_equity"] and "roe_pct" not in n and "pb" not in n
    assert balance_ratios([snap(date(2025, 12, 31), 50)], None) == {}


# -- ingest & backfill ---------------------------------------------------------------------------------------------
def test_ingest_stores_balance_sheet_and_backfill_fills_old_rows():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    pe = date(2025, 9, 30)
    doc = with_bs(xbrl(pe.isoformat()), **COMPANY_BS)
    src = FakeResults({"a": (filing("UP0", pe, True, "a"), doc)})
    ingest_symbol(f, src, "UP0", quarters=4)
    with f() as s:
        row = s.scalar(select(m.FundamentalSnapshot))
        assert row.equity == Decimal("900.00") and row.total_debt == Decimal("500.00") and row.raw["bs_kind"] == "company"
    with f() as s, s.begin():                                   # simulate a row stored before balance sheets were parsed
        row = s.scalar(select(m.FundamentalSnapshot))
        row.equity = row.total_debt = None
    out = backfill_balance_sheets(f, src)
    assert out == {"checked": 1, "filled": 1, "no_balance_sheet": 0, "invalid": 0, "errors": 0}
    with f() as s:
        assert s.scalar(select(m.FundamentalSnapshot.equity)) == Decimal("900.00")


# -- shareholding XBRL (both formats) --------------------------------------------------------------------------------
def shp_new(promoter=0.7177, public=0.2823, fpi=0.0906, dii=0.1347, mf=0.0568, enc=None, holders=2605182):
    def t(tag, ctx, v):
        return f'<in-bse-shp:{tag} contextRef="{ctx}" decimals="INF" unitRef="pure">{v}</in-bse-shp:{tag}>'
    pct = "ShareholdingAsAPercentageOfTotalNumberOfShares"
    body = [t(pct, "ShareholdingPattern_ContextI", 1), t("NumberOfShareholders", "ShareholdingPattern_ContextI", holders),
            t(pct, "ShareholdingOfPromoterAndPromoterGroup_ContextI", promoter), t(pct, "PublicShareholding_ContextI", public),
            t(pct, "InstitutionsForeign_ContextI", fpi), t(pct, "InstitutionsDomestic_ContextI", dii),
            t(pct, "MutualFundsOrUTI_ContextI", mf),
            t(pct, "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakh_ContextI", 0.047),
            t(pct, "ResidentIndividualShareholdersHoldingNominalShareCapitalInExcessOfRsTwoLakh_ContextI", 0.0021),
            t("WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged", "MainI", "false" if enc is None else "true"),
            t("PermanentAccountNumberOfShareholder", "Shareholder1I", "ABCDE1234F")]
    if enc is not None:
        body.append(t("EncumberedSharesHeldAsPercentageOfTotalNumberOfShares", "ShareholdingOfPromoterAndPromoterGroup_ContextI", enc))
    return "<xbrli:xbrl>" + "".join(body) + "</xbrli:xbrl>"


def test_parse_shp_new_and_old_formats():
    new = parse_shp(shp_new(enc=0.25))
    assert new["promoter_pct"] == Decimal("71.7700") and new["mf_pct"] == Decimal("5.6800") and new["retail_pct"] == Decimal("4.9100")
    assert new["promoter_encumbered_pct"] == Decimal("25.0000") and new["promoter_pledged_pct"] is None and new["shareholders"] == 2605182
    assert parse_shp(shp_new())["promoter_pledged_pct"] == 0
    assert "ABCDE1234F" not in str(new)                                                  # PANs are never carried along
    old = ("<x>" + '<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="ShareholdingPatternI">100</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>'
           + '<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="ShareholdingOfPromoterAndPromoterGroupI">71.77</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>'
           + '<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="PublicShareholdingI">28.23</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>'
           + '<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="MutualFundsOrUtiI">4.32</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>'
           + '<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="GovermentsI">0.5</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>'
           + '<in-bse-shp:PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares contextRef="ShareholdingOfPromoterAndPromoterGroupI">3.5</in-bse-shp:PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares>'
           + "</x>")
    o = parse_shp(old)
    assert o["promoter_pct"] == Decimal("71.7700") and o["mf_pct"] == Decimal("4.3200") and o["government_pct"] == Decimal("0.5000")
    assert o["promoter_encumbered_pct"] == Decimal("3.5000")


def test_parse_master_and_checks():
    rows = parse_master([
        {"symbol": "UP0", "isin": "INE000", "date": "30-JUN-2026", "broadcastDate": "21-JUL-2026 15:43:50", "revisedData": "N",
         "xbrl": "https://x/1.xml", "pr_and_prgrp": "71.77", "public_val": "28.23"},
        {"symbol": "UP0", "isin": "INE000", "date": "07-JUL-2026", "broadcastDate": "16-JUL-2026 20:25:37", "xbrl": "https://x/2.xml"},
        {"symbol": "UP0", "isin": "INE000", "date": "31-MAR-2026", "broadcastDate": "16-SEP-2026 19:38:06", "revisedData": "Revised",
         "xbrl": "https://x/3.xml", "pr_and_prgrp": "71.77"}])
    assert [(r.period_end, r.revised) for r in rows] == [(date(2026, 6, 30), False), (date(2026, 3, 31), True)]
    assert rows[1].published_at == datetime(2026, 9, 16, 19, 38, 6)
    good = parse_shp(shp_new())
    assert check_pattern(good, rows[0]) == []
    assert any("sum to" in i for i in check_pattern({**good, "public_pct": Decimal(10)}, rows[0]))
    assert any("exceed the public float" in i for i in check_pattern({**good, "fpi_pct": Decimal(40)}, rows[0]))
    assert any("exchange summary" in i for i in check_pattern({**good, "promoter_pct": Decimal(60), "public_pct": Decimal(40)}, rows[0]))


class FakeShp:
    def __init__(self, filings, docs):
        self.f, self.docs, self.fetched = filings, docs, []

    def filings(self, symbol):
        return self.f

    def summary(self, x):
        self.fetched.append(x.xbrl)
        return parse_shp(self.docs[x.xbrl])


def shp_filing(pe, pub, url, isin="INE000A01001", revised=False, promoter="71.77"):
    return ShpFiling("UP0", isin, pe, pub, url, revised, Decimal(promoter), None)


def test_ingest_shareholding_revisions_and_isin():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    with f() as s, s.begin():
        s.scalar(select(m.Instrument).where(m.Instrument.symbol == "UP0")).isin = "INE000A01001"
    q1, q2 = date(2026, 3, 31), date(2026, 6, 30)
    docs = {"a": shp_new(), "b": shp_new(promoter=0.70, public=0.30, fpi=0.12), "p": shp_new(promoter=0.10, public=0.90),
            "r": shp_new(enc=0.30)}
    src = FakeShp([shp_filing(q1, datetime(2026, 4, 18, 16), "a"), shp_filing(q2, datetime(2026, 7, 21, 15), "b", promoter="70"),
                   shp_filing(q2, datetime(2026, 7, 21, 15), "p", isin="IN9000A01001", promoter="10")], docs)
    rep = ingest_sh(f, src, "UP0")
    assert rep.stored == 2 and not rep.issues and "p" not in src.fetched              # partly-paid line ignored
    src.f = [shp_filing(q1, datetime(2026, 9, 16, 19), "r", revised=True)]          # revision filed in September
    rep = ingest_sh(f, src, "UP0")
    assert rep.updated == 1
    with f() as s:
        r = s.scalar(select(m.Shareholding).where(m.Shareholding.period_end == q1))
        assert r.revised and r.promoter_encumbered_pct == Decimal("30.0000") and r.published_at.astimezone(IST).month == 9
    assert ingest_sh(f, src, "UP0").skipped_existing == 1                            # unchanged: nothing re-fetched


def test_get_shareholding_tool_point_in_time_and_flags():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    with f() as s, s.begin():
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == "UP0"))
        for pe, pub, prom, enc in ((date(2025, 12, 31), datetime(2026, 1, 20, 18, tzinfo=IST), 72, 10),
                                   (date(2026, 3, 31), datetime(2026, 4, 20, 18, tzinfo=IST), 70.5, 30),
                                   (date(2026, 6, 30), datetime(2026, 7, 20, 18, tzinfo=IST), 69, 40)):
            s.add(m.Shareholding(instrument_id=iid, period_end=pe, published_at=pub, promoter_pct=Decimal(str(prom)),
                                 public_pct=Decimal(str(100 - prom)), fpi_pct=Decimal(10), dii_pct=Decimal(12),
                                 promoter_encumbered_pct=Decimal(enc)))
    svc = make_service(f)
    r = call(svc, "fundamental", "get_shareholding", {"symbol": "UP0"}, as_of=datetime(2026, 5, 1, 18, 30, tzinfo=IST))
    assert r.ok and [q["period_end"] for q in r.data["quarters"]] == ["2026-03-31", "2025-12-31"]   # June not published yet
    assert r.data["changes"]["vs_previous_quarter"]["promoter_pct"] == -1.5
    assert any("encumbered" in x for x in r.data["flags"]) and any("promoter holding fell" in x for x in r.data["flags"])
    early = call(svc, "fundamental", "get_shareholding", {"symbol": "UP0"}, as_of=datetime(2025, 12, 1, 18, tzinfo=IST))
    assert early.ok is False


def test_sector_medians_compare_like_with_like():
    _, f = synthetic_db(n_up=4, n_flat=0, n_down=0, days=20)
    with f() as s, s.begin():
        ids = list(s.scalars(select(m.Instrument.id).where(m.Instrument.symbol.like("UP%"))))
        for i, iid in enumerate(ids):
            kind = "bank" if i < 3 else "company"
            for q in quarters(kind=kind):
                s.add(m.FundamentalSnapshot(instrument_id=iid, period_end=q.period_end, period_type="Q", published_at=q.published_at,
                                            revenue=q.revenue, pat=q.pat, eps=q.eps, equity=q.equity, total_debt=q.total_debt,
                                            raw=q.raw, source="nse_xbrl_consolidated"))
    with f() as s:
        banks = sector_medians(s, ids, date(2026, 6, 1), kind="bank")
        cos = sector_medians(s, ids, date(2026, 6, 1), kind="company")
    assert banks["roe_pct"] is not None and banks.get("debt_equity") is None
    assert cos.get("roe_pct") is None                                              # only one company peer: below 3


def test_no_promoter_group_and_esop_trust_filers():
    pct = "ShareholdingAsAPercentageOfTotalNumberOfShares"
    t = lambda ctx, v: f'<in-bse-shp:{pct} contextRef="{ctx}">{v}</in-bse-shp:{pct}>'
    no_promoter = parse_shp("<x>" + t("ShareholdingPattern_ContextI", 1) + t("PublicShareholding_ContextI", 1) + "</x>")
    assert no_promoter["promoter_pct"] == 0 and no_promoter["no_promoter_group"]
    trust = parse_shp("<x>" + t("ShareholdingPattern_ContextI", 1) + t("ShareholdingOfPromoterAndPromoterGroup_ContextI", 0.18)
                      + t("PublicShareholding_ContextI", 0.785) + t("EmployeeBenefitsTrusts_ContextI", 0.035) + "</x>")
    assert trust["non_promoter_non_public_pct"] == Decimal("3.5000")
    with_promoter = ShpFiling("X", "I", date(2026, 6, 30), datetime(2026, 7, 20), "u", False, Decimal("18"), None)
    without = ShpFiling("X", "I", date(2026, 6, 30), datetime(2026, 7, 20), "u", False, None, None)
    assert check_pattern(trust, with_promoter) == [] and check_pattern(no_promoter, without) == []
