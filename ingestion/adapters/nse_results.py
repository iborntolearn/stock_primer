"""NSE quarterly results as XBRL (the fundamentals source for phase 4).

Two listings cover the history:
- corporates-financial-results: filings up to Dec 2024 (tags prefixed in-bse-fin)
- integrated-filing-results: SEBI's integrated filing from 2025 (tags prefixed in-capmkt)
Both use the same tag names and a "OneD" context for the quarter, so one parser
reads both. XBRL gives the printed numbers directly, so no LLM is needed to read
them; LLM-15 is kept for filings that only exist as PDF text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from .base import DiskCache, PermanentError, TokenBucket, utcnow, with_retries

LEGACY = "https://www.nseindia.com/api/corporates-financial-results"
INTEGRATED = "https://www.nseindia.com/api/integrated-filing-results"

# Each field lists tag alternatives in order: companies first, then the banking taxonomy.
FIELDS = {
    # revenue: companies, banks, then insurers (life: net premium; general: net earned premium)
    "revenue": ("RevenueFromOperations", "InterestEarned", "NetPremiumIncome", "PremiumEarned"), "other_income": "OtherIncome", "income": "Income", "expenses": "Expenses",
    "finance_costs": "FinanceCosts", "depreciation": "DepreciationDepletionAndAmortisationExpense",
    "exceptional": "ExceptionalItemsBeforeTax",
    "pbt": ("ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax", "ProfitLossBeforeTax", "ProfitOrLossBeforeTax"),
    # insurers: life files the shareholders' account tax as ProvisionsForTaxes (ProvisionForTax there is the
    # policyholders' account), general insurers as ProvisionForTax, so the order matters
    "tax": ("TaxExpense", "ProvisionsForTaxes", "ProvisionForTax"),
    "pat": ("ProfitLossForPeriod", "ProfitLossForThePeriod", "ProfitLossAfterTaxAndExtraordinaryItems", "ProfitLossAfterTax"),
    "pat_owners": ("ProfitOrLossAttributableToOwnersOfParent", "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates"),
    "eps_basic": ("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                  "BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShareAfterExtraordinaryItems",
                  "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"),
    "paid_up_capital": "PaidUpValueOfEquityShareCapital", "face_value": "FaceValueOfEquityShareCapital",
    "bank_operating_profit": "OperatingProfitBeforeProvisionAndContingencies",
    "interest_expended": "InterestExpended",
    # Consolidated profit after tax also includes these, added below the tax line
    "associates": ("ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod", "ShareOfProfitLossOfAssociates"),
    "regulatory": "NetMovementInRegulatoryDeferralAccountBalancesRelatedToProfitOrLossAndTheRelatedDeferredTaxMovement",
    "discontinued": "ProfitLossFromDiscontinuedOperationsAfterTax",
    "nci": "ProfitOrLossAttributableToNonControllingInterests",
}
# Insurer metrics (quarter): premium mix and the ratios analysts use instead of margins
INSURER_FIELDS = {
    "ins_gross_premium": ("GrossPremiumIncome", "GrossPremiumsWritten"), "ins_net_premium_written": ("NetPremiumWritten", "NetPremiumIncome"),
    "ins_first_year_premium": "IncomeFirstYearPremium", "ins_renewal_premium": "IncomeRenewalPremium",
    "ins_single_premium": "IncomeSinglePremium", "ins_underwriting_profit": "UnderwritingProfitOrLoss",
    "ins_solvency_ratio": "SolvencyRatio", "ins_expense_ratio": "ExpensesOfManagementRatio",
    "ins_combined_ratio": "CombinedRatio", "ins_claims_ratio": "IncurredClaimRatio",
}

TEXT_FIELDS = {
    "period_start": "DateOfStartOfReportingPeriod", "period_end": "DateOfEndOfReportingPeriod",
    "nature": "NatureOfReportStandaloneConsolidated", "audited": "WhetherResultsAreAuditedOrUnaudited",
    "board_meeting": "DateOfBoardMeetingWhenFinancialResultsWereApproved", "symbol": "Symbol",
}


# Balance sheet ("OneI" instant context; filed with the half-year and year-end results)
BS_FIELDS = {
    "bs_equity_owners": "EquityAttributableToOwnersOfParent", "bs_equity_total": "Equity",
    "bs_share_capital": ("EquityShareCapital", "Capital", "ShareCapital"), "bs_other_equity": ("OtherEquity", "ReservesAndSurplus"),
    "bs_shareholders_funds": "ShareholdersFunds",
    "bs_borrowings_noncurrent": "BorrowingsNoncurrent", "bs_borrowings_current": "BorrowingsCurrent",
    "bs_borrowings": "Borrowings", "bs_debt_securities": "DebtSecurities", "bs_subordinated": "SubordinatedLiabilities",
    "bs_deposits": "Deposits", "bs_assets": "Assets",
    "bs_cash": ("CashAndCashEquivalents", "CashAndBalancesWithReserveBankOfIndia"),
    "bs_advances": "Advances", "bs_loans": "Loans",
}


def balance_sheet(q: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalised balance sheet in rupees, or None if the filing has none.
    kind: bank (Capital + Reserves, Advances), nbfc (Ind-AS financial format: Loans/DebtSecurities), company."""
    assets = q.get("bs_assets")
    has_equity = any(q.get(k) is not None for k in ("bs_equity_owners", "bs_equity_total", "bs_shareholders_funds", "bs_share_capital"))
    if assets is None or not has_equity:
        return None                                   # quarter-only filings (insurers file a few asset lines quarterly)
    if q.get("is_insurer"):
        kind = "insurer"                              # before the lender test: life insurers also show "Loans"
    elif q.get("bs_loans") is not None or q.get("bs_debt_securities") is not None:
        kind = "nbfc"                                  # Ind-AS financial-company format
    elif q.get("bs_advances") is not None or (q.get("is_bank") and q.get("bs_deposits") is not None):
        kind = "bank"                                  # banking format (Schedule III for banks)
    else:
        kind = "company"
    capital_reserves = None
    if q.get("bs_share_capital") is not None and q.get("bs_other_equity") is not None:
        capital_reserves = q["bs_share_capital"] + q["bs_other_equity"]
    funds = q.get("bs_shareholders_funds")
    if funds is not None and capital_reserves is not None and abs(funds - capital_reserves) > Decimal("0.1") * abs(capital_reserves):
        funds = None                                   # misfiled (e.g. -562 cr against 16,846 cr capital + reserves)
    owners = q.get("bs_equity_owners") or funds or capital_reserves
    total = q.get("bs_equity_total") or owners
    z = Decimal(0)
    if kind == "insurer":
        debt = q.get("bs_borrowings")                  # policy liabilities are the business, not leverage
    elif kind == "company":
        debt = (q.get("bs_borrowings_noncurrent") or z) + (q.get("bs_borrowings_current") or z) + (q.get("bs_borrowings") or z)
    elif kind == "nbfc":
        debt = sum((q.get(k) or z for k in ("bs_debt_securities", "bs_borrowings", "bs_subordinated", "bs_deposits")), z)
    else:
        debt = q.get("bs_borrowings")                    # banks: deposits are the business, not leverage
    # Negative equity is real (accumulated losses), not a data error; equity above total assets is.
    # Insurers (general insurers especially) present assets net of current liabilities, so equity can exceed "Assets".
    if owners is None or total is None or (total > assets and kind != "insurer") or (debt is not None and debt < 0):
        return {"kind": kind, "invalid": True}
    return {"kind": kind, "equity_owners": owners, "equity_total": total, "debt": debt, "assets": assets,
            "cash": q.get("bs_cash")}


def _num(x: Optional[str]) -> Optional[Decimal]:
    try:
        return Decimal(x.strip()) if x not in (None, "") else None
    except InvalidOperation:
        return None


def parse_xbrl(text: str) -> dict[str, Any]:
    """Quarter ("OneD" context) values. Money stays in rupees exactly as filed."""
    def first(tag: str) -> Optional[str]:
        m = re.search(rf'<[a-z\-]+:{tag}\b[^>]*contextRef="OneD"[^>]*>([^<]*)<', text)
        return m.group(1).strip() if m else None

    out: dict[str, Any] = {}
    for k, tags in FIELDS.items():
        tags = (tags,) if isinstance(tags, str) else tags
        out[k] = next((v for v in (_num(first(t)) for t in tags) if v is not None), None)
    out["is_bank"] = out["bank_operating_profit"] is not None or first("InterestEarned") is not None
    for k, tags in INSURER_FIELDS.items():
        tags = (tags,) if isinstance(tags, str) else tags
        out[k] = next((v for v in (_num(first(t)) for t in tags) if v is not None), None)
    out["is_insurer"] = out["ins_gross_premium"] is not None
    if out["is_insurer"]:
        out["insurer_type"] = "life" if first("NetPremiumIncome") is not None else "general"

    def instant(tag: str) -> Optional[str]:
        m = re.search(rf'<[a-z\-]+:{tag}\b[^>]*contextRef="OneI"[^>]*>([^<]*)<', text)
        return m.group(1).strip() if m else None
    for k, tags in BS_FIELDS.items():
        tags = (tags,) if isinstance(tags, str) else tags
        out[k] = next((v for v in (_num(instant(t)) for t in tags) if v is not None), None)
    if out.get("paid_up_capital") is None:              # insurers file paid-up capital as a balance-sheet item
        out["paid_up_capital"] = next((v for v in (_num(instant(t)) for t in ("PaidUpEquityShareCapital", "PaidUpEquityCapital"))
                                       if v is not None), None)
    for k, t in TEXT_FIELDS.items():
        out[k] = first(t)
    for k in ("period_start", "period_end", "board_meeting"):
        try:
            out[k] = date.fromisoformat(out[k]) if out[k] else None
        except ValueError:
            out[k] = None
    return out


@dataclass
class ResultFiling:
    symbol: str
    period_end: date
    consolidated: bool
    published_at: datetime
    xbrl_url: str
    source: str            # nse_legacy | nse_integrated


def _dt(s: str) -> Optional[datetime]:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def parse_listing(payload: Any, source: str) -> list[ResultFiling]:
    rows = payload.get("data", []) if isinstance(payload, dict) else (payload or [])
    out = []
    for r in rows:
        url = r.get("xbrl")
        if not url or url.endswith("/null"):
            continue
        if source == "nse_integrated":
            if "Financials" not in (r.get("type") or ""):
                continue
            pe, pub, cons = _dt(r.get("qe_Date", "").title()), _dt(r.get("broadcast_Date") or r.get("creation_Date") or ""), r.get("consolidated")
        else:
            if r.get("period") not in (None, "Quarterly"):
                continue
            pe, pub, cons = _dt(r.get("toDate", "")), _dt(r.get("broadCastDate") or r.get("filingDate") or ""), r.get("consolidated")
        if pe is None or pub is None:
            continue
        out.append(ResultFiling(r["symbol"], pe.date(), cons == "Consolidated", pub, url, source))
    return out


class NseResults:
    name = "nse_results"

    def __init__(self, cache: DiskCache, *, get_json: Optional[Callable[[str, dict], Any]] = None,
                 get_text: Optional[Callable[[str], str]] = None, bucket: Optional[TokenBucket] = None):
        self.cache = cache
        self.bucket = bucket or TokenBucket(rate=0.5)
        self._get_json = get_json or self._live_json
        self._get_text = get_text or self._live_text
        self._live, self._arch = None, None

    def _live_json(self, url: str, params: dict) -> Any:
        if self._live is None:
            from jugaad_data.nse import NSELive

            self._live = NSELive()
        r = self._live.s.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def _live_text(self, url: str) -> str:
        if self._arch is None:
            from jugaad_data.nse.archives import NSEArchives

            self._arch = NSEArchives()       # nsearchives needs the archive session's headers (403 otherwise)
        r = self._arch.s.get(url, timeout=30)
        if r.status_code == 404:
            raise PermanentError(f"{url}: not found")
        r.raise_for_status()
        return r.text

    def filings(self, symbol: str) -> list[ResultFiling]:
        out = []
        for url, params, source in ((INTEGRATED, {"index": "equities", "symbol": symbol}, "nse_integrated"),
                                    (LEGACY, {"index": "equities", "period": "Quarterly", "symbol": symbol}, "nse_legacy")):
            def go(url=url, params=params):
                self.bucket.acquire()
                return self._get_json(url, params)
            out += parse_listing(with_retries(go, attempts=2), source)
        return out

    def xbrl(self, f: ResultFiling) -> str:
        key = {"url": f.xbrl_url}
        cached = self.cache.get(self.name, "xbrl", key, f.period_end, "xml")
        if cached is not None:
            return cached

        def go():
            self.bucket.acquire()
            return self._get_text(f.xbrl_url)
        text = with_retries(go, attempts=2)
        self.cache.put(self.name, "xbrl", key, f.period_end, text, "xml")
        return text
