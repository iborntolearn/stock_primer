"""Delivery-trade cost model from the versioned cost_config (§4.6). Rates change: verify before trusting results."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m


def rates_for(s: Session, day: date) -> dict[str, Any]:
    row = s.scalar(select(m.CostConfig).where(m.CostConfig.effective_from <= day)
                   .order_by(m.CostConfig.effective_from.desc(), m.CostConfig.version.desc()).limit(1))
    if row is None:
        raise RuntimeError("no cost_config row; run `stock-primer-data seed`")
    return {**row.rates, "version": row.version}


def trade_costs(side: str, qty: int, price: float, r: dict[str, Any]) -> dict[str, float]:
    turnover = qty * price
    brokerage = min(turnover * r["brokerage_pct"] / 100, r["brokerage_max_per_order"])
    stt = turnover * r["stt_delivery_pct"] / 100
    exch = turnover * r["exchange_txn_pct"] / 100
    sebi = turnover * r["sebi_fee_per_crore"] / 1e7
    gst = (brokerage + exch + sebi) * r["gst_pct"] / 100
    stamp = turnover * r["stamp_duty_buy_pct"] / 100 if side == "BUY" else 0.0
    dp = r["dp_charge_per_sell_scrip"] if side == "SELL" and qty > 0 else 0.0
    parts = {"brokerage": brokerage, "stt": stt, "exch_charges": exch + sebi, "gst": gst, "stamp_duty": stamp,
             "dp_charges": dp}
    parts = {k: round(v, 2) for k, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 2)
    return parts


def slippage_bps(order_value: float, adv_value: float, r: dict[str, Any]) -> float:
    """Base bps plus bps per 1% of 20-day average traded value the order represents."""
    pct_adv = 100 * order_value / adv_value if adv_value else 0.0
    return r["slippage_bps_base"] + r["slippage_bps_per_pct_adv"] * pct_adv
