"""Point-in-time fundamental metrics from fundamental_snapshot (quarterly, from NSE XBRL).

Only snapshots published on or before the decision date are used: growth (revenue / profit
YoY), quality (operating and net margin, earnings consistency, ROE), valuation (trailing P/E,
earnings yield, P/B), and balance sheet (from half-year filings): ROE, ROCE, debt/equity and
net debt for companies; ROE and assets/equity for banks and NBFCs, where debt/equity means little.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m


def _f(x) -> Optional[float]:
    return None if x is None else float(x)


def load_snapshots(s: Session, iids: list[int], as_of: date, quarters: int = 9) -> dict[int, list[m.FundamentalSnapshot]]:
    """Latest quarterly snapshot per period (consolidated preferred), newest first."""
    rows = s.scalars(select(m.FundamentalSnapshot).where(
        m.FundamentalSnapshot.instrument_id.in_(iids), m.FundamentalSnapshot.period_type == "Q",
        m.FundamentalSnapshot.published_at <= as_of)).all()
    by: dict[int, dict[date, m.FundamentalSnapshot]] = defaultdict(dict)
    for r in rows:
        cur = by[r.instrument_id].get(r.period_end)
        if cur is None or ("consolidated" in r.source and "consolidated" not in cur.source):
            by[r.instrument_id][r.period_end] = r
    return {iid: sorted(d.values(), key=lambda r: r.period_end, reverse=True)[:quarters] for iid, d in by.items()}


def _year_ago(snaps: list[m.FundamentalSnapshot], pe: date) -> Optional[m.FundamentalSnapshot]:
    return next((r for r in snaps if abs((r.period_end - pe.replace(year=pe.year - 1)).days) <= 5), None)


def metrics(snaps: list[m.FundamentalSnapshot], price: Optional[float]) -> dict[str, Any]:
    if not snaps:
        return {}
    q = snaps[0]
    ya = _year_ago(snaps, q.period_end)
    rev, pat = _f(q.revenue), _f(q.pat)
    out: dict[str, Any] = {
        "latest_quarter": q.period_end.isoformat(), "published_at": q.published_at.isoformat(), "source": q.source,
        "revenue_cr": rev, "pat_cr": pat, "eps": _f(q.eps), "op_margin_pct": _f(q.op_margin),
        "pat_margin_pct": round(pat / rev * 100, 2) if rev and pat is not None else None,
        "revenue_yoy_pct": round((rev / float(ya.revenue) - 1) * 100, 2) if ya and ya.revenue and rev else None,
        "pat_yoy_pct": round((pat / float(ya.pat) - 1) * 100, 2) if ya and ya.pat and float(ya.pat) > 0 and pat is not None else None,
        "earnings_consistency": round(sum(1 for r in snaps[:8] if r.pat is not None and float(r.pat) > 0) / min(len(snaps), 8), 2),
        "quarters_available": len(snaps),
    }
    out.update(balance_ratios(snaps, price))
    ins = insurer_metrics(snaps)
    if ins:
        out["insurer"] = ins
        out.pop("op_margin_pct", None)                # no operating margin for insurers
    last4 = snaps[:4]
    if len(last4) == 4 and all(r.eps is not None for r in last4) and (last4[0].period_end - last4[3].period_end).days < 300:
        ttm = ttm_eps(last4)
        out["ttm_eps"] = round(ttm, 2)
        if price and ttm > 0:
            out["pe"] = round(price / ttm, 2)
            out["earnings_yield_pct"] = round(ttm / price * 100, 3)
    return out


CR = 1e7


def _raw(r, key: str) -> Optional[float]:
    v = (r.raw or {}).get(key)
    try:
        return None if v in (None, "None", "") else float(v)
    except (TypeError, ValueError):
        return None


def _solvency(v: Optional[float]) -> Optional[float]:
    """Solvency ratio as a multiple (regulatory minimum 1.5). Some filers tag 2.42 as 0.0242."""
    if v is None or v <= 0:
        return None
    return round(v * 100 if v < 0.15 else v, 2)


def insurer_metrics(snaps: list[m.FundamentalSnapshot]) -> dict[str, Any]:
    """What insurance analysts look at instead of margins: premium growth and mix, solvency, cost and claims ratios."""
    if not snaps or str((snaps[0].raw or {}).get("is_insurer")) != "True":
        return {}
    q = snaps[0]
    ya = _year_ago(snaps, q.period_end)
    gp, gp_ya = _raw(q, "ins_gross_premium"), _raw(ya, "ins_gross_premium") if ya else None
    out = {"type": (q.raw or {}).get("insurer_type"), "gross_premium_cr": round(gp / CR, 2) if gp else None,
           "gross_premium_yoy_pct": round((gp / gp_ya - 1) * 100, 2) if gp and gp_ya else None,
           "solvency_ratio": _solvency(_raw(q, "ins_solvency_ratio"))}
    for key, src in (("expense_of_management_ratio_pct", "ins_expense_ratio"), ("combined_ratio_pct", "ins_combined_ratio"),
                     ("claims_ratio_pct", "ins_claims_ratio")):
        v = _raw(q, src)
        out[key] = None if v is None else round(v * 100 if v < 5 else v, 2)   # filed as fractions (1.072 = 107.2%)
    if (q.raw or {}).get("insurer_type") == "life":
        fy, rn, sg = (_raw(q, k) for k in ("ins_first_year_premium", "ins_renewal_premium", "ins_single_premium"))
        if gp and None not in (fy, rn, sg):
            out["new_business_share_pct"] = round((fy + sg) / gp * 100, 2)     # first-year + single premium
    return {k: v for k, v in out.items() if v is not None}


def _raw_cr(r: m.FundamentalSnapshot, key: str) -> Optional[float]:
    v = (r.raw or {}).get(key)
    try:
        return None if v in (None, "None", "") else float(v) / CR
    except (TypeError, ValueError):
        return None


def _ttm(snaps: list[m.FundamentalSnapshot], value) -> Optional[float]:
    """Sum over the latest four quarters, only if they are four consecutive quarters."""
    last4 = snaps[:4]
    if len(last4) < 4 or (last4[0].period_end - last4[3].period_end).days > 300:
        return None
    vals = [value(r) for r in last4]
    return None if any(v is None for v in vals) else sum(vals)


def balance_ratios(snaps: list[m.FundamentalSnapshot], price: Optional[float]) -> dict[str, Any]:
    """Ratios from the latest published balance sheet (half-yearly), averaged with the one a year earlier if present."""
    bs = [r for r in snaps if r.equity is not None]
    if not bs:
        return {}
    latest = bs[0]
    if float(latest.equity) <= 0:                      # accumulated losses: ROE, D/E and P/B are meaningless
        return {"balance_sheet_as_of": latest.period_end.isoformat(), "equity_cr": round(float(latest.equity), 2),
                "total_debt_cr": _f(latest.total_debt), "negative_equity": True}
    bs = [r for r in bs if float(r.equity) > 0]
    kind = (latest.raw or {}).get("bs_kind") or "company"
    year_ago = next((r for r in bs[1:] if 300 <= (latest.period_end - r.period_end).days <= 430), None)

    def avg(fn):
        a, b = fn(latest), fn(year_ago) if year_ago else None
        return None if a is None else (a + b) / 2 if b is not None else a
    eq_owners = avg(lambda r: float(r.equity))
    out: dict[str, Any] = {"balance_sheet_as_of": latest.period_end.isoformat(), "balance_sheet_kind": kind,
                           "equity_cr": round(float(latest.equity), 2),
                           "total_debt_cr": _f(latest.total_debt), "averaged_with_year_ago": year_ago is not None}
    ttm_pat = _ttm(snaps, lambda r: _f(r.pat))
    if ttm_pat is not None and eq_owners:
        out["roe_pct"] = round(ttm_pat / eq_owners * 100, 2)
    total_eq = lambda r: _raw_cr(r, "bs_equity_total") or float(r.equity)
    if kind == "company":
        debt = _f(latest.total_debt)
        if debt is not None:
            out["debt_equity"] = round(debt / total_eq(latest), 2)
            cash = _raw_cr(latest, "bs_cash")
            if cash is not None:
                out["net_debt_cr"] = round(debt - cash, 2)
        ttm_ebit = _ttm(snaps, lambda r: None if _raw_cr(r, "pbt") is None else _raw_cr(r, "pbt") + (_raw_cr(r, "finance_costs") or 0))
        cap = avg(lambda r: total_eq(r) + (_f(r.total_debt) or 0))
        if ttm_ebit is not None and cap:
            out["roce_pct"] = round(ttm_ebit / cap * 100, 2)
    elif kind in ("bank", "nbfc"):                      # insurers: leverage is read from the solvency ratio instead
        assets = _raw_cr(latest, "bs_assets")
        if assets:
            out["assets_to_equity"] = round(assets / total_eq(latest), 2)
    cap_paid, fv = _raw_cr(latest, "paid_up_capital"), (latest.raw or {}).get("face_value")
    try:
        shares = cap_paid * CR / float(fv) if cap_paid and fv not in (None, "None", "") and float(fv) > 0 else None
    except ValueError:
        shares = None
    if shares is None:                                 # no face value filed (insurers): shares = quarter profit / EPS
        q0 = snaps[0]
        if q0.eps is not None and float(q0.eps) > 0 and q0.pat is not None and float(q0.pat) > 0:
            shares = float(q0.pat) * CR / float(q0.eps)
    if price and shares:
        out["pb"] = round(price * shares / (float(latest.equity) * CR), 2)
    return out


def last_close(s: Session, iid: int, d: date) -> Optional[float]:
    v = s.scalar(select(m.PriceDaily.close).where(m.PriceDaily.instrument_id == iid, m.PriceDaily.trade_date <= d)
                 .order_by(m.PriceDaily.trade_date.desc()).limit(1))
    return _f(v)


def _shares(r: m.FundamentalSnapshot) -> Optional[float]:
    """Share count from paid-up capital / face value (paid-up alone if face value isn't filed: same-face-value proxy)."""
    pu, fv = _raw(r, "paid_up_capital"), _raw(r, "face_value")
    return pu / fv if pu and fv else pu


def ttm_eps(window: list[m.FundamentalSnapshot]) -> float:
    """Sum of four quarters' EPS on the share basis of the newest one: after a bonus or split, older quarters'
    EPS is rescaled (LIC's 1:1 bonus would otherwise roughly double trailing EPS and halve P/E)."""
    base = _shares(window[0])
    total = 0.0
    for r in window:
        e, sh = float(r.eps), _shares(r)
        if base and sh and abs(base / sh - 1) > 0.05:
            e *= sh / base
        total += e
    return total


def pe_history(s: Session, snaps: list[m.FundamentalSnapshot], iid: int) -> list[float]:
    """Trailing P/E at each past quarter's publication date (own-history comparison)."""
    out = []
    for i in range(len(snaps) - 3):
        window = snaps[i:i + 4]
        if any(r.eps is None for r in window):
            continue
        ttm = ttm_eps(window)
        px = last_close(s, iid, window[0].published_at)
        if px and ttm > 0:
            out.append(round(px / ttm, 2))
    return out


BS_KEYS = ("roe_pct", "roce_pct", "debt_equity", "pb")


def sector_medians(s: Session, iids: list[int], as_of: date, kind: Optional[str] = None) -> dict[str, Optional[float]]:
    """Peer medians (need 3+ peers). With `kind`, balance-sheet ratios only come from peers of the same kind,
    so a bank isn't compared with the non-lenders that share its sector."""
    snaps = load_snapshots(s, iids, as_of)
    vals: dict[str, list[float]] = defaultdict(list)
    for iid, rows in snaps.items():
        mt = metrics(rows, last_close(s, iid, as_of))
        for k in ("op_margin_pct", "pat_margin_pct", "revenue_yoy_pct", "pe") + BS_KEYS:
            if mt.get(k) is None or (k in BS_KEYS and kind and mt.get("balance_sheet_kind") != kind):
                continue
            vals[k].append(mt[k])
    return {k: (round(median(v), 2) if len(v) >= 3 else None) for k, v in vals.items()} | {"peers": len(snaps)}
