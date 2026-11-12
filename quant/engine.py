"""Quant engine facade used by quant-mcp and market-mcp (§4.4).

Outputs follow the contract the orchestrator relies on:
  score_horizon / screen_universe rows: symbol, sector, quant_score (0-100), confidence, gates{},
      gates_passed, rank, above_200dma, atr, sector_index, group_scores, data_gaps
  propose_levels: entry_low, entry_high, stop, target, rr, method
  regime_inputs: quant_label, score + the readings behind them
One cross-sectional snapshot per decision date is computed and cached.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m
from core.settings import Settings, get_settings
from ingestion.reference import universe_members

from . import factors as fx
from . import indicators as ind
from .data import load_bars, load_index
from .fundamentals import load_snapshots
from .fundamentals import metrics as fund_metrics

TICK = 0.05
MIN_BARS = {"short": 60, "medium": 130, "long": 200}
SECTOR_INDEX = {
    "Financial Services": "NIFTY FINANCIAL SERVICES", "Information Technology": "NIFTY IT",
    "Automobile and Auto Components": "NIFTY AUTO", "Fast Moving Consumer Goods": "NIFTY FMCG",
    "Healthcare": "NIFTY HEALTHCARE INDEX", "Metals & Mining": "NIFTY METAL",
    "Oil Gas & Consumable Fuels": "NIFTY OIL & GAS", "Realty": "NIFTY REALTY", "Power": "NIFTY POWER",
    "Capital Goods": "NIFTY CAPITAL GOODS", "Consumer Durables": "NIFTY CONSUMER DURABLES",
    "Chemicals": "NIFTY CHEMICALS", "Construction Materials": "NIFTY CEMENT", "Construction": "NIFTY CONSTRUCTION",
    "Telecommunication": "NIFTY TELECOMMUNICATIONS", "Media Entertainment & Publication": "NIFTY MEDIA",
    "Consumer Services": "NIFTY CONSUMER SERVICES",
}
BENCH = "NIFTY 500"


class UnknownSymbol(Exception):
    pass


def tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


@dataclass
class Snapshot:
    as_of: date
    members: dict[int, str]
    sectors: dict[int, Optional[str]]
    bars: dict[int, pd.DataFrame]
    raw: pd.DataFrame
    z: pd.DataFrame
    stats: pd.DataFrame
    index_close: pd.Series
    excluded: set[str]
    regime: dict[str, Any]
    horizon_raw: dict[str, pd.Series] = field(default_factory=dict)
    sector_events: dict[str, float] = field(default_factory=dict)     # net event overlay per sector
    stock_events: dict[int, float] = field(default_factory=dict)


IST = timezone(timedelta(hours=5, minutes=30))


def _series(s: Session, series_id: str, as_of: date, n: int = 30) -> pd.Series:
    rows = s.execute(select(m.MacroSeries.obs_date, m.MacroSeries.value).where(
        m.MacroSeries.series_id == series_id, m.MacroSeries.obs_date <= as_of)
        .order_by(m.MacroSeries.obs_date.desc()).limit(n)).all()[::-1]
    return pd.Series([float(v) for _, v in rows], index=[d for d, _ in rows], dtype=float)


def event_overlay(s: Session, as_of: date) -> tuple[dict[str, float], dict[int, float]]:
    """Net event impact per sector (and per stock) as of a date (section 4.4.1 event overlay).

    Each impact contributes direction x magnitude x confidence, decayed linearly to zero
    over the event's decay_days. Only events first seen by the decision date count.
    """
    cutoff = datetime.combine(as_of, time(23, 59), IST)
    sectors: dict[str, float] = {}
    stocks: dict[int, float] = {}
    q = (select(m.EventImpact, m.Event, m.Sector.name)
         .join(m.Event, m.Event.id == m.EventImpact.event_id)
         .outerjoin(m.Sector, m.Sector.id == m.EventImpact.sector_id)
         .where(m.Event.status.in_(("active", "unverified"))))
    for imp, ev, sector in s.execute(q):
        if ev.first_seen is not None and ev.first_seen > cutoff:
            continue
        details = imp.details or {}
        decay = float(details.get("decay_days") or 10)
        seen = ev.first_seen.astimezone(IST).date() if ev.first_seen else as_of
        weight = max(0.0, 1 - (as_of - seen).days / decay) if decay > 0 else 0.0
        if weight <= 0:
            continue
        conf = float(details.get("confidence") or ev.confidence or 0.5)
        if imp.instrument_id is not None and imp.sector_id is None:
            stocks[imp.instrument_id] = stocks.get(imp.instrument_id, 0.0) + 0.5 * imp.direction * weight * conf
        elif sector:
            sectors[sector] = sectors.get(sector, 0.0) + imp.direction * float(imp.magnitude) * conf * weight
    return ({k: round(v, 4) for k, v in sectors.items()}, {k: round(v, 4) for k, v in stocks.items()})


class QuantEngine:
    def __init__(self, factory: sessionmaker[Session], settings: Optional[Settings] = None):
        self.f = factory
        self.settings = settings or get_settings()
        self._cache: dict[date, Snapshot] = {}
        self._lock = threading.Lock()

    # -- snapshot ------------------------------------------------------------------
    def snapshot(self, as_of: date) -> Snapshot:
        with self._lock:
            if as_of not in self._cache:
                self._cache[as_of] = self._build(as_of)
            return self._cache[as_of]

    def _build(self, as_of: date) -> Snapshot:
        with self.f() as s:
            members = universe_members(s, self.settings.universe, as_of)
            sectors = {}
            for iid in members:
                inst = s.get(m.Instrument, iid)
                sectors[iid] = s.get(m.Sector, inst.sector_id).name if inst.sector_id else inst.industry
            bars = load_bars(s, list(members), as_of)
            index_close = load_index(s, BENCH, as_of)
            gate = s.get(m.DataGateResult, as_of)
            excluded = set(gate.excluded) if gate else set()
            vix = load_index(s, "INDIA VIX", as_of, 30)
            sector_events, stock_events = event_overlay(s, as_of)
            snaps = load_snapshots(s, list(members), as_of)
            macro = {sid: _series(s, sid, as_of) for sid in ("USDINR", "BRENT", "US10Y")}
            flows = s.execute(select(m.FlowsDaily.trade_date, m.FlowsDaily.fii_net).where(m.FlowsDaily.trade_date <= as_of)
                              .order_by(m.FlowsDaily.trade_date.desc()).limit(5)).all()

        rows = {iid: fx.raw_factors(bars.get(iid), index_close) for iid in members}
        for iid in members:
            b = bars.get(iid)
            price = float(b["close"].iloc[-1]) if b is not None and len(b) else None
            if rows[iid]:   # no price history -> no row at all (fundamentals alone do not make a candidate)
                rows[iid] = {**rows[iid], **fx.fundamental_factors(fund_metrics(snaps.get(iid, []), price))}
        raw = pd.DataFrame.from_dict(rows, orient="index")
        if not raw.empty and "ret_3m" in raw:
            sec = pd.Series(sectors).reindex(raw.index)
            raw["sector_mom_3m"] = raw.groupby(sec)["ret_3m"].transform("median")
        if not raw.empty:
            raw["event_net"] = [sector_events.get(sectors.get(i) or "", 0.0) + stock_events.get(i, 0.0) for i in raw.index]
        z, stats = fx.zscore_table(raw) if not raw.empty else (raw, pd.DataFrame())
        regime = self._regime(as_of, bars, index_close, vix, macro, flows)
        snap = Snapshot(as_of, members, sectors, bars, raw, z, stats, index_close, excluded, regime,
                        sector_events=sector_events, stock_events=stock_events)
        for h in fx.WEIGHTS:
            snap.horizon_raw[h] = pd.Series(
                {iid: fx.horizon_raw(fx.group_scores(self._zrow(snap, iid))[0], h) for iid in raw.index}, dtype=float)
        return snap

    @staticmethod
    def _zrow(snap: Snapshot, iid: int) -> dict[str, float]:
        if iid in snap.z.index:
            return {k: float(v) for k, v in snap.z.loc[iid].items()}
        return {}

    # -- regime (§4.4, B3) -----------------------------------------------------------
    def _regime(self, as_of: date, bars: dict[int, pd.DataFrame], idx: pd.Series, vix: pd.Series,
                macro: Optional[dict[str, pd.Series]] = None, flows: Optional[list] = None) -> dict[str, Any]:
        macro = macro or {}
        trend, trend_score = "unknown", 0.0
        if len(idx) >= 200:
            s50, s200 = ind.sma(idx, 50), ind.sma(idx, 200)
            up = idx.iloc[-1] > s200.iloc[-1] and ind.slope(s50).iloc[-1] > 0
            down = idx.iloc[-1] < s200.iloc[-1] and ind.slope(s50).iloc[-1] < 0
            trend, trend_score = ("up", 1.0) if up else ("down", -1.0) if down else ("sideways", 0.0)
        above = [b["close"].iloc[-1] > ind.sma(b["close"], 200).iloc[-1] for b in bars.values() if len(b) >= 200]
        breadth = 100 * sum(above) / len(above) if above else None
        vix_last = float(vix.iloc[-1]) if len(vix) else None
        vix_chg5 = float(vix.iloc[-1] / vix.iloc[-6] - 1) * 100 if len(vix) >= 6 else None
        parts, weights = [], []
        if trend != "unknown":
            parts.append(trend_score); weights.append(0.4)
        if breadth is not None:
            parts.append(max(-1, min(1, (breadth - 50) / 25))); weights.append(0.3)
        if vix_last is not None:
            parts.append(max(-1, min(1, (16 - vix_last) / 6))); weights.append(0.3)
        unavailable = []
        fii_5d = sum(float(v or 0) for _, v in flows) if flows else None
        if fii_5d is not None:
            parts.append(max(-1, min(1, fii_5d / 10000))); weights.append(0.15)   # 10k crore net in 5 days = full signal
        else:
            unavailable.append("fii_flows")

        def chg(sid: str, pct: bool = True) -> Optional[float]:
            ser = macro.get(sid)
            if ser is None or len(ser) < 6:
                return None
            return float((ser.iloc[-1] / ser.iloc[-6] - 1) * 100) if pct else float(ser.iloc[-1] - ser.iloc[-6])
        usdinr, brent, us10y_chg = chg("USDINR"), chg("BRENT"), chg("US10Y", pct=False)
        # A weakening rupee, rising crude and rising US yields all lean risk-off.
        for name, val, scale, w in (("usdinr", usdinr, 1.0, 0.10), ("brent", brent, 7.0, 0.05), ("us10y", us10y_chg, 0.25, 0.05)):
            if val is None:
                unavailable.append(name)
            else:
                parts.append(max(-1, min(1, -val / scale))); weights.append(w)
        score = sum(p * w for p, w in zip(parts, weights)) / sum(weights) if weights else 0.0
        label = "risk_on" if score > 0.25 else "risk_off" if score < -0.25 else "neutral"
        return {"as_of": as_of.isoformat(), "quant_label": label, "score": round(score, 3),
                "nifty500_trend": trend, "nifty500_close": round(float(idx.iloc[-1]), 2) if len(idx) else None,
                "breadth_pct_above_200dma": round(breadth, 1) if breadth is not None else None,
                "india_vix": vix_last, "india_vix_chg_5d_pct": round(vix_chg5, 2) if vix_chg5 is not None else None,
                "fii_5d_cr": round(fii_5d, 2) if fii_5d is not None else None,
                "usdinr_chg_5d_pct": round(usdinr, 2) if usdinr is not None else None,
                "brent_chg_5d_pct": round(brent, 2) if brent is not None else None,
                "us10y": round(float(macro["US10Y"].iloc[-1]), 3) if len(macro.get("US10Y", [])) else None,
                "us10y_chg_5d": round(us10y_chg, 3) if us10y_chg is not None else None,
                "unavailable": unavailable}

    # -- per-symbol --------------------------------------------------------------------
    def _resolve(self, snap: Snapshot, symbol: str) -> tuple[int, pd.DataFrame, Optional[str]]:
        for iid, sym in snap.members.items():
            if sym == symbol:
                return iid, snap.bars.get(iid), snap.sectors.get(iid)
        with self.f() as s:
            inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == symbol))
            if inst is None:
                raise UnknownSymbol(symbol)
            sector = s.get(m.Sector, inst.sector_id).name if inst.sector_id else inst.industry
            bars = load_bars(s, [inst.id], snap.as_of).get(inst.id)
        return inst.id, bars, sector

    def _z_for(self, snap: Snapshot, iid: int, bars: pd.DataFrame, sector: Optional[str]) -> dict[str, float]:
        if iid in snap.z.index:
            return self._zrow(snap, iid)
        raw = fx.raw_factors(bars, snap.index_close)
        with self.f() as s:
            snaps = load_snapshots(s, [iid], snap.as_of).get(iid, [])
        price = float(bars["close"].iloc[-1]) if bars is not None and len(bars) else None
        raw.update(fx.fundamental_factors(fund_metrics(snaps, price)))
        raw["event_net"] = snap.sector_events.get(sector or "", 0.0) + snap.stock_events.get(iid, 0.0)
        if not snap.raw.empty and "ret_3m" in snap.raw:
            sec = pd.Series(snap.sectors).reindex(snap.raw.index)
            same = snap.raw["ret_3m"][sec == sector]
            raw["sector_mom_3m"] = float(same.median()) if len(same) else float("nan")
        return fx.z_against(raw, snap.stats) if not snap.stats.empty else {}

    def compute_indicators(self, symbol: str, as_of: date) -> dict[str, Any]:
        snap = self.snapshot(as_of)
        _, bars, _ = self._resolve(snap, symbol)
        if bars is None or bars.empty:
            raise UnknownSymbol(f"{symbol}: no price history")
        return {"symbol": symbol, "as_of": bars.index[-1].isoformat(), **ind.latest(bars)}

    def factor_scores(self, symbol: str, as_of: date) -> dict[str, Any]:
        snap = self.snapshot(as_of)
        iid, bars, sector = self._resolve(snap, symbol)
        z = self._z_for(snap, iid, bars, sector)
        groups, gaps = fx.group_scores(z)
        return {"symbol": symbol, "factor_z": {k: (round(v, 3) if not math.isnan(v) else None) for k, v in z.items()},
                "group_scores": {k: round(v, 3) for k, v in groups.items()}, "data_gaps": gaps}

    def _gates(self, snap: Snapshot, symbol: str, iid: int, bars: pd.DataFrame, horizon: str) -> dict[str, bool]:
        c = bars["close"]
        adv = bars["traded_value"].rolling(20, min_periods=10).mean().iloc[-1]
        s200 = ind.sma(c, 200).iloc[-1] if len(c) >= 200 else float("nan")
        with self.f() as s:
            pending = s.scalar(select(m.CorporateAction.id).where(
                m.CorporateAction.instrument_id == iid, m.CorporateAction.action_type.in_(("demerger", "merger")),
                m.CorporateAction.ex_date > snap.as_of, m.CorporateAction.ex_date <= snap.as_of + timedelta(days=10)))
        gates = {
            "liquidity": bool(adv >= self.settings.min_avg_traded_value_cr * 1e7) if not math.isnan(adv) else False,
            "price_floor": bool(c.iloc[-1] >= self.settings.min_price),
            "history": len(c) >= MIN_BARS[horizon],
            "data_quality": symbol not in snap.excluded,
            "no_pending_restructuring": pending is None,
        }
        if horizon == "short":
            gates["regime"] = snap.regime["quant_label"] != "risk_off"
        if horizon == "long":
            gates["above_200dma"] = bool(not math.isnan(s200) and c.iloc[-1] > s200)
        return gates

    def score_horizon(self, symbol: str, horizon: str, as_of: date) -> dict[str, Any]:
        snap = self.snapshot(as_of)
        iid, bars, sector = self._resolve(snap, symbol)
        if bars is None or len(bars) < 21:
            raise UnknownSymbol(f"{symbol}: not enough price history")
        z = self._z_for(snap, iid, bars, sector)
        groups, gaps = fx.group_scores(z)
        raw_score = fx.horizon_raw(groups, horizon)
        dist = snap.horizon_raw.get(horizon, pd.Series(dtype=float)).dropna()
        pct = float((dist < raw_score).mean() * 100) if len(dist) else 50.0
        gates = self._gates(snap, symbol, iid, bars, horizon)
        miss = fx.missing_weight(gaps, horizon)
        confidence = max(0.2, min(0.8, 0.8 - 0.6 * miss))
        c = bars["close"]
        s200 = ind.sma(c, 200).iloc[-1] if len(c) >= 200 else float("nan")
        a = ind.atr(bars).iloc[-1]
        rank = int((dist > raw_score).sum() + 1) if len(dist) else None
        return {
            "symbol": symbol, "sector": sector, "horizon": horizon, "quant_score": round(pct, 2),
            "raw_score": round(raw_score, 4), "confidence": round(confidence, 3), "gates": gates,
            "gates_passed": all(gates.values()), "rank": rank, "universe_size": int(len(dist)),
            "above_200dma": None if math.isnan(s200) else bool(c.iloc[-1] > s200),
            "atr": round(float(a), 2) if not math.isnan(a) else None,
            "sector_index": SECTOR_INDEX.get(sector or "", BENCH),
            "group_scores": {k: round(v, 3) for k, v in groups.items()}, "data_gaps": gaps,
            "missing_weight": round(miss, 3),
        }

    def screen_universe(self, horizon: str, as_of: date, top_k: int = 15,
                        constraints: Optional[dict] = None) -> dict[str, Any]:
        snap = self.snapshot(as_of)
        constraints = constraints or {}
        rows, rejected = [], {}
        for iid, sym in snap.members.items():
            if constraints.get("sector") and snap.sectors.get(iid) != constraints["sector"]:
                continue
            try:
                r = self.score_horizon(sym, horizon, as_of)
            except UnknownSymbol:
                continue
            if r["gates_passed"]:
                rows.append(r)
            else:
                for g, ok in r["gates"].items():
                    if not ok:
                        rejected[g] = rejected.get(g, 0) + 1
        overlay = constraints.get("sector_overlay") or {}
        for r in rows:  # sector overlay from the Sector agent nudges ranking only within the quant score
            r["screen_score"] = r["quant_score"] + 5 * float(overlay.get(r["sector"], 0.0))
        rows.sort(key=lambda r: r["screen_score"], reverse=True)
        return {"horizon": horizon, "as_of": as_of.isoformat(), "candidates": rows[:top_k],
                "eligible": len(rows), "universe_size": len(snap.members), "gate_rejections": rejected,
                "regime": snap.regime["quant_label"]}

    def propose_levels(self, symbol: str, horizon: str, as_of: date) -> dict[str, Any]:
        """§4.4.3. Prices on the NSE 0.05 tick; rr is computed here, never by the LLM."""
        snap = self.snapshot(as_of)
        _, bars, _ = self._resolve(snap, symbol)
        if bars is None or len(bars) < 21:
            raise UnknownSymbol(f"{symbol}: not enough price history")
        c = bars["close"]
        last = float(c.iloc[-1])
        a = float(ind.atr(bars).iloc[-1])
        if math.isnan(a) or a <= 0:
            a = last * 0.02
        s20 = float(ind.sma(c, 20).iloc[-1])
        s50 = float(ind.sma(c, 50).iloc[-1]) if len(c) >= 50 else s20
        swing10 = float(bars["low"].rolling(10, min_periods=5).min().iloc[-1])
        swing20 = float(bars["low"].rolling(20, min_periods=10).min().iloc[-1])
        if horizon == "short":
            entry_low, entry_high = last - 0.5 * a, last + 0.5 * a
            atr_stop, structural = entry_low - 1.75 * a, swing10 - 0.1 * a
            stop = min(max(atr_stop, structural), entry_low - 1.0 * a)   # tighter, but not inside noise
            rr_mult, method = 2.0, "ATR band; stop max(1.75 ATR, 10-day swing low), at least 1 ATR"
        elif horizon == "medium":
            entry_low = max(min(s20, last) - 0.25 * a, last - 1.5 * a)
            entry_high = last + 0.25 * a
            stop = min(min(s50, swing20) - 0.5 * a, entry_low - 1.5 * a)
            rr_mult, method = 2.5, "20-DMA band; stop below 50-DMA / 20-day swing low"
        else:
            entry_low, entry_high = last - 1.0 * a, last + 1.0 * a
            stop = entry_low * (1 - 0.22)
            rr_mult, method = 1.5, "price-led (no valuation range yet); 22% drawdown stop"
        entry_low, entry_high, stop = tick(entry_low), tick(entry_high), tick(stop)
        risk = entry_high - stop
        target = tick(entry_high + rr_mult * risk)
        rr = round((target - entry_high) / risk, 2) if risk > 0 else 0.0
        return {"symbol": symbol, "horizon": horizon, "entry_low": entry_low, "entry_high": entry_high,
                "stop": stop, "target": target, "rr": rr, "atr": round(a, 2), "reference_close": round(last, 2),
                "method": method}

    def regime_inputs(self, as_of: date) -> dict[str, Any]:
        return self.snapshot(as_of).regime

    def market_breadth(self, as_of: date) -> dict[str, Any]:
        snap = self.snapshot(as_of)
        adv = dec = above50 = above200 = n50 = n200 = hi = lo = 0
        for b in snap.bars.values():
            if len(b) < 2 or b.index[-1] != as_of:
                continue
            c = b["close"]
            adv += c.iloc[-1] > c.iloc[-2]
            dec += c.iloc[-1] < c.iloc[-2]
            if len(c) >= 50:
                n50 += 1; above50 += c.iloc[-1] > ind.sma(c, 50).iloc[-1]
            if len(c) >= 200:
                n200 += 1; above200 += c.iloc[-1] > ind.sma(c, 200).iloc[-1]
            if len(c) >= 120:
                hi += c.iloc[-1] >= b["high"].rolling(252, min_periods=120).max().iloc[-1]
                lo += c.iloc[-1] <= b["low"].rolling(252, min_periods=120).min().iloc[-1]
        return {"as_of": as_of.isoformat(), "universe": self.settings.universe, "advances": int(adv),
                "declines": int(dec), "pct_above_50dma": round(100 * above50 / n50, 1) if n50 else None,
                "pct_above_200dma": round(100 * above200 / n200, 1) if n200 else None,
                "new_52w_highs": int(hi), "new_52w_lows": int(lo)}

    @staticmethod
    def suggest_position_size(portfolio_value: float, entry: float, stop: float, risk_pct: float = 0.75,
                              max_position_pct: float = 10.0) -> dict[str, Any]:
        risk_per_share = entry - stop
        if risk_per_share <= 0:
            return {"qty": 0, "reason": "stop not below entry"}
        qty_risk = math.floor(portfolio_value * risk_pct / 100 / risk_per_share)
        qty_cap = math.floor(portfolio_value * max_position_pct / 100 / entry)
        qty = max(0, min(qty_risk, qty_cap))
        return {"qty": qty, "position_value": round(qty * entry, 2),
                "position_pct": round(100 * qty * entry / portfolio_value, 2) if portfolio_value else 0,
                "binding": "risk" if qty_risk <= qty_cap else "position_cap", "simulation_only": True}
