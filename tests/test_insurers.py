"""Insurer XBRL (life and general): statement mapping, balance sheet with misfiled shareholders' funds,
insurer metrics (premium growth, solvency scale, combined ratio), and bonus-adjusted trailing EPS."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ingestion.adapters.nse_results import balance_sheet, parse_xbrl
from ingestion.fundamentals import check_statement, reconcile_owners, to_snapshot
from quant.fundamentals import _solvency, insurer_metrics, ttm_eps

from .test_phase4 import CR, filing


def tag(name, v, ctx="OneD"):
    return f'<in-capmkt:{name} contextRef="{ctx}" unitRef="INR" decimals="-5">{v}</in-capmkt:{name}>'


def life(pe="2026-06-30", bs=False, **over):
    v = dict(GrossPremiumIncome=17232 * CR, NetPremiumIncome=16728 * CR, IncomeFirstYearPremium=2746 * CR,
             IncomeRenewalPremium=9033 * CR, IncomeSinglePremium=5453 * CR, ProvisionForTax=47 * CR,       # policyholders' tax
             ProfitLossBeforeTax=630 * CR, ProvisionsForTaxes=19 * CR, ProfitLossAfterTaxAndExtraordinaryItems=611 * CR,
             BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized="2.83",
             SolvencyRatio="1.85", ExpensesOfManagementRatio="0.2255", DateOfEndOfReportingPeriod=pe,
             NatureOfReportStandaloneConsolidated="Consolidated")
    v.update(over)
    body = "".join(tag(k, x) for k, x in v.items()) + tag("PaidUpEquityShareCapital", 2172 * CR, "OneI") + tag("Assets", 404796 * CR, "OneI")
    if bs:
        body += "".join(tag(k, x, "OneI") for k, x in dict(ShareCapital=2158 * CR, ReservesAndSurplus=15302 * CR,
                                                          ShareholdersFunds=17750 * CR, Borrowings=3099 * CR, Loans=2827 * CR).items())
    return "<xbrli:xbrl>" + body + "</xbrli:xbrl>"


def general(**over):
    v = dict(GrossPremiumsWritten=8860 * CR, NetPremiumWritten=6604 * CR, PremiumEarned=5950 * CR, OtherIncome=6 * CR,
             ProfitOrLossBeforeTax=536 * CR, ProvisionForTax=133 * CR, ProfitLossAfterTax=403 * CR,
             BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized="8.08",
             SolvencyRatio="0.0271", CombinedRatio="1.072", IncurredClaimRatio="0.764", ExpensesOfManagementRatio="0.288",
             DateOfEndOfReportingPeriod="2026-06-30", NatureOfReportStandaloneConsolidated="Standalone")
    v.update(over)
    bs = "".join(tag(k, x, "OneI") for k, x in dict(ShareCapital=498 * CR, ReservesAndSurplus=16347 * CR, ShareholdersFunds=-562 * CR,
                                                   Borrowings=0, Assets=16072 * CR, PaidUpEquityCapital=498 * CR).items())
    return "<xbrli:xbrl>" + "".join(tag(k, x) for k, x in v.items()) + bs + "</xbrli:xbrl>"


def test_life_insurer_mapping_and_checks():
    q = reconcile_owners(parse_xbrl(life()))
    assert q["is_insurer"] and q["insurer_type"] == "life"
    assert q["revenue"] == 16728 * CR and q["pbt"] == 630 * CR and q["tax"] == 19 * CR and q["pat"] == 611 * CR   # shareholders' tax
    assert q["eps_basic"] == Decimal("2.83") and q["paid_up_capital"] == 2172 * CR
    assert check_statement(q) == []
    assert balance_sheet(q) is None                            # quarterly filing: a few asset lines but no equity
    b = balance_sheet(parse_xbrl(life(pe="2026-03-31", bs=True)))
    assert b["kind"] == "insurer" and b["equity_owners"] == 17750 * CR and b["debt"] == 3099 * CR   # not mistaken for an NBFC
    snap = to_snapshot(1, filing("X", date(2026, 6, 30), True, "u"), q)
    assert snap.ebitda is None and snap.op_margin is None and snap.revenue == Decimal("16728.00")


def test_general_insurer_with_misfiled_shareholders_funds():
    q = parse_xbrl(general())
    assert q["insurer_type"] == "general" and q["revenue"] == 5950 * CR and q["tax"] == 133 * CR and check_statement(q) == []
    b = balance_sheet(q)
    assert b["kind"] == "insurer" and b["equity_owners"] == (498 + 16347) * CR and not b.get("invalid")   # -562 cr ignored
    assert b["debt"] == 0                                       # net presentation: equity > "Assets" is fine for insurers


def test_insurer_metrics_and_solvency_scale():
    def snap(pe, gp):
        return SimpleNamespace(period_end=pe, raw={"is_insurer": "True", "insurer_type": "general", "ins_gross_premium": str(gp * CR),
                                                   "ins_solvency_ratio": "0.0271", "ins_combined_ratio": "1.072",
                                                   "ins_claims_ratio": "0.764", "ins_expense_ratio": "0.288"})
    out = insurer_metrics([snap(date(2026, 6, 30), 8860), snap(date(2025, 6, 30), 8053)])
    assert out["gross_premium_yoy_pct"] == pytest.approx(10.02, abs=0.01) and out["solvency_ratio"] == 2.71
    assert out["combined_ratio_pct"] == 107.2 and out["claims_ratio_pct"] == 76.4 and out["expense_of_management_ratio_pct"] == 28.8
    assert _solvency(1.85) == 1.85 and _solvency(0.0242) == 2.42 and _solvency(None) is None
    assert insurer_metrics([SimpleNamespace(period_end=date(2026, 6, 30), raw={})]) == {}


def test_ttm_eps_adjusts_for_bonus_issue():
    def q(pe, eps, paid_up):
        return SimpleNamespace(period_end=pe, eps=Decimal(str(eps)), raw={"paid_up_capital": str(paid_up)})
    window = [q(date(2026, 6, 30), 10.74, 1265e8), q(date(2026, 3, 31), 37.10, 632.5e8),
              q(date(2025, 12, 31), 20.44, 632.5e8), q(date(2025, 9, 30), 15.97, 632.5e8)]
    assert ttm_eps(window) == pytest.approx(10.74 + (37.10 + 20.44 + 15.97) / 2, abs=0.001)       # 1:1 bonus in June
    same = [q(date(2026, 6, 30), 10, 100), q(date(2026, 3, 31), 10, 100), q(date(2025, 12, 31), 10, 100), q(date(2025, 9, 30), 10, 100)]
    assert ttm_eps(same) == 40
