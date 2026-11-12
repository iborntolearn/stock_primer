"""What-if scenarios and stress tests (B6, D10). Deterministic; no LLM.

A scenario is a set of shocks: the market (NIFTY 500, %), Brent, USD/INR or gold (%), or the
US 10-year yield (percentage points). Sizes come from weekly history:

- market move: given, or implied by regressing the market on the shocked factors;
- sector move: sector-index beta to the market times the market move, plus the sector's own
  (beyond-market) sensitivity to each factor;
- stock move: the stock's market beta (shrunk towards 1) times the market move, plus its
  sector's beyond-market sensitivity.

A sensitivity counts only when |t| >= T_MIN; weaker links are reported, then left out. The
approved transmission map is shown next to each sector as a cross-check, not used for sizing.
The historical replay needs no model: it applies the worst real N-day window to today's holdings.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from core import models as m
from quant.data import load_bars, load_index
from quant.engine import BENCH, SECTOR_INDEX

T_MIN = 1.5
MIN_WEEKS = 26
LOOKBACK_DAYS = 760
FACTORS = {  # code -> (label, unit)
    "market": ("NIFTY 500", "%"), "BRENT": ("Brent crude", "%"), "USDINR": ("USD/INR", "%"),
    "US10Y": ("US 10-year yield", "pp"), "GOLD": ("Gold", "%"),
}
MAP_FACTOR = {"BRENT": "crude_up", "USDINR": "usdinr_up", "US10Y": "us10y_up", "GOLD": "gold_up"}
PRESETS = {
    "crude_spike": {"name": "Crude spikes 25%", "shocks": [{"factor": "BRENT", "change": 25.0}]},
    "rupee_slide": {"name": "Rupee weakens 4%", "shocks": [{"factor": "USDINR", "change": 4.0}]},
    "us_yields_up": {"name": "US 10Y yield +0.75 pp", "shocks": [{"factor": "US10Y", "change": 0.75}]},
    "correction": {"name": "Market falls 10%", "shocks": [{"factor": "market", "change": -10.0}]},
    "risk_off": {"name": "Global risk-off", "shocks": [{"factor": "market", "change": -7.0}, {"factor": "USDINR", "change": 2.5},
                                                       {"factor": "BRENT", "change": 12.0}, {"factor": "US10Y", "change": 0.4}]},
}


@dataclass
class Shock:
    factor: str
    change: float          # % for prices, percentage points for US10Y

    def __post_init__(self):
        if self.factor not in FACTORS:
            raise ValueError(f"unknown factor {self.factor}; use one of {', '.join(FACTORS)}")
        if not math.isfinite(self.change) or abs(self.change) > (5 if self.factor == "US10Y" else 80):
            raise ValueError(f"{self.factor} change {self.change} is out of range")


# -- free text -----------------------------------------------------------------------------------
_WORDS = [("BRENT", r"crude|brent|oil"), ("USDINR", r"rupee|usd\s*/?\s*inr|inr|dollar"), ("US10Y", r"us\s*(?:10\s*-?\s*y\w*|yields?|treasur\w*|bond)|treasur\w*"),
          ("GOLD", r"gold"), ("market", r"nifty|sensex|market|stocks")]
_DOWN = r"\b(?:falls?|drops?|down|crash(?:es)?|declines?|slumps?|plunges?|lower|cut|tumbles?|slides?|sinks?)\b"
_RUPEE_WEAK = r"\b(?:weak\w*|falls?|drops?|slumps?|slides?|depreciat\w*|plunges?|down|sinks?)\b"
_RUPEE_STRONG = r"\b(?:strong\w*|strength\w*|rises?|gains?|appreciat\w*|up|rall(?:y|ies))\b"


def parse_scenario(text: str, latest: dict[str, float]) -> tuple[list[Shock], list[str]]:
    """'What if crude hits $110 and the rupee falls 3%?' -> shocks. Notes explain anything assumed or skipped."""
    shocks: dict[str, Shock] = {}
    notes: list[str] = []
    for c in re.split(r"\band\b|;|\bplus\b|\bwith\b", text.lower().replace(",", "")):
        hit = next(((code, mt) for code, pat in _WORDS if (mt := re.search(pat, c))), None)
        if not hit:
            continue
        code, mt = hit
        label, unit_name = FACTORS[code]
        if code == "USDINR" and re.search(r"rupee|inr", mt.group(0)) and not re.search(r"usd|dollar", mt.group(0)):
            down = bool(re.search(_RUPEE_STRONG, c)) and not re.search(_RUPEE_WEAK, c)   # a weaker rupee = USD/INR up
        else:
            down = bool(re.search(_DOWN, c))
        after = c[mt.end():]
        num = re.search(r"(-?\d+(?:\.\d+)?)\s*(%|percent|pc|bps?|basis points?|pp)?", after)
        if not num:
            notes.append(f"No size given for {label}; skipped.")
            continue
        val, unit = float(num.group(1)), (num.group(2) or "").strip()
        sign = -1 if (down or val < 0) else 1
        level = re.search(r"\b(?:hits?|to|at|reach\w*|touch\w*|cross\w*|above|below)\b\s*\$?\s*-?\d", after)
        try:
            if unit.startswith("b"):
                if code != "US10Y":
                    notes.append(f"Basis points only make sense for yields; skipped {label}.")
                    continue
                change = sign * abs(val) / 100
            elif unit in ("%", "percent", "pc", "pp") and not level:
                change = sign * abs(val)
            elif level or code == "US10Y":
                last = latest.get(code)
                if last is None:
                    notes.append(f"No recent {label} level is stored, so a target level can't be converted; skipped.")
                    continue
                change = (val - last) if code == "US10Y" else (val / last - 1) * 100
                notes.append(f"{label} {last:,.2f} → {val:,.2f} is {change:+.2f}{unit_name}.")
            else:
                change = sign * abs(val)
            shocks[code] = Shock(code, round(change, 4))
        except ValueError as e:
            notes.append(str(e))
    return list(shocks.values()), notes


# -- statistics ------------------------------------------------------------------------------------
def weekly(series: pd.Series, unit: str = "%") -> pd.Series:
    """Friday-to-Friday change: % for prices, difference for yields."""
    if series.empty:
        return series
    s = series.copy()
    s.index = pd.to_datetime(s.index)
    w = s.resample("W-FRI").last().dropna()
    return (w.diff() if unit == "pp" else w.pct_change() * 100).dropna()


@dataclass
class Fit:
    coef: dict[str, float]
    t: dict[str, float]
    n: int
    r2: float

    def use(self, k: str) -> float:
        """Coefficient if statistically meaningful, else 0."""
        return self.coef.get(k, 0.0) if abs(self.t.get(k, 0.0)) >= T_MIN else 0.0


def ols(y: pd.Series, X: pd.DataFrame) -> Optional[Fit]:
    df = pd.concat([y.rename("_y"), X], axis=1, join="inner").dropna()
    if len(df) < MIN_WEEKS or X.shape[1] == 0:
        return None
    A = np.column_stack([np.ones(len(df)), df[X.columns].to_numpy(float)])
    b = df["_y"].to_numpy(float)
    coef, *_ = np.linalg.lstsq(A, b, rcond=None)
    resid = b - A @ coef
    dof = max(1, len(b) - A.shape[1])
    sigma2 = float(resid @ resid) / dof
    try:
        cov = sigma2 * np.linalg.inv(A.T @ A)
        se = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
    except np.linalg.LinAlgError:
        return None
    ss_tot = float(((b - b.mean()) ** 2).sum()) or 1.0
    names = list(X.columns)
    return Fit({k: float(coef[i + 1]) for i, k in enumerate(names)},
               {k: float(coef[i + 1] / se[i + 1]) for i, k in enumerate(names)}, len(b), 1 - float(resid @ resid) / ss_tot)


# -- engine ----------------------------------------------------------------------------------------
@dataclass
class Holding:
    portfolio_id: int
    symbol: str
    iid: int
    sector: Optional[str]
    qty: int
    last: float
    stop: Optional[float]
    target: Optional[float]

    @property
    def value(self) -> float:
        return self.qty * self.last


@dataclass
class StressResult:
    as_of: str
    scenario: dict[str, Any]
    market: dict[str, Any]
    sectors: list[dict[str, Any]]
    portfolios: list[dict[str, Any]]
    open_picks: list[dict[str, Any]]
    assumptions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class StressEngine:
    def __init__(self, s: Session, as_of: Optional[date] = None, lookback_days: int = LOOKBACK_DAYS):
        self.s = s
        self.as_of = as_of or s.scalar(select(m.PriceDaily.trade_date).order_by(m.PriceDaily.trade_date.desc()).limit(1))
        self.lookback = lookback_days
        self._cache: dict[str, pd.Series] = {}

    # inputs
    def latest_levels(self) -> dict[str, float]:
        out = {}
        for code in FACTORS:
            ser = self._level(code)
            if not ser.empty:
                out[code] = float(ser.iloc[-1])
        return out

    def _level(self, code: str) -> pd.Series:
        if code in self._cache:
            return self._cache[code]
        if code == "market":
            ser = load_index(self.s, BENCH, self.as_of, self.lookback)
        else:
            rows = self.s.execute(select(m.MacroSeries.obs_date, m.MacroSeries.value).where(
                m.MacroSeries.series_id == code, m.MacroSeries.obs_date <= self.as_of,
                m.MacroSeries.obs_date >= self.as_of - timedelta(days=self.lookback)).order_by(m.MacroSeries.obs_date)).all()
            ser = pd.Series([float(v) for _, v in rows], index=[d for d, _ in rows], dtype=float)
        self._cache[code] = ser
        return ser

    def _weekly_factor(self, code: str) -> pd.Series:
        return weekly(self._level(code), FACTORS[code][1]).rename(code)

    def _sectors(self) -> dict[int, Optional[str]]:
        return {i.id: (sec.name if sec else None) for i, sec in self.s.execute(
            select(m.Instrument, m.Sector).outerjoin(m.Sector, m.Sector.id == m.Instrument.sector_id))}

    def holdings(self, portfolio_ids: Optional[list[int]]) -> tuple[list[Holding], dict[int, dict]]:
        q = select(m.Position, m.Instrument).join(m.Instrument, m.Instrument.id == m.Position.instrument_id).where(
            m.Position.status == "open")
        pq = select(m.Portfolio).where(m.Portfolio.archived.is_(False))
        if portfolio_ids:
            q, pq = q.where(m.Position.portfolio_id.in_(portfolio_ids)), pq.where(m.Portfolio.id.in_(portfolio_ids))
        rows = self.s.execute(q).all()
        ports = {p.id: {"portfolio_id": p.id, "name": p.name, "type": p.type, "cash": float(p.cash)} for p in self.s.scalars(pq)}
        sectors = self._sectors()
        bars = load_bars(self.s, sorted({i.id for _, i in rows}), self.as_of, 30)
        out = []
        for pos, inst in rows:
            b = bars.get(inst.id)
            last = float(b["close"].iloc[-1]) if b is not None and not b.empty else float(pos.avg_cost)
            out.append(Holding(pos.portfolio_id, inst.symbol, inst.id, sectors.get(inst.id), pos.qty, last,
                               float(pos.stop) if pos.stop is not None else None,
                               float(pos.target) if pos.target is not None else None))
        return out, ports

    def open_picks(self) -> list[dict[str, Any]]:
        rows = self.s.execute(select(m.Recommendation, m.Instrument).join(m.Instrument, m.Instrument.id == m.Recommendation.instrument_id)
                              .where(m.Recommendation.status.in_(("published", "active")), m.Recommendation.verdict == "favourable")).all()
        sectors = self._sectors()
        bars = load_bars(self.s, sorted({i.id for _, i in rows}), self.as_of, 30)
        out = []
        for r, i in rows:
            b = bars.get(i.id)
            if b is None or b.empty:
                continue
            out.append({"reco_id": r.id, "symbol": i.symbol, "iid": i.id, "sector": sectors.get(i.id), "horizon": r.horizon,
                        "last": float(b["close"].iloc[-1]), "stop": float(r.stop) if r.stop is not None else None})
        return out

    # model
    def _market_move(self, shocks: list[Shock], notes: list[str]) -> dict[str, Any]:
        given = next((x for x in shocks if x.factor == "market"), None)
        if given:
            return {"move_pct": given.change, "basis": "given", "links": []}
        fs = [x.factor for x in shocks]
        X = pd.concat([self._weekly_factor(f) for f in fs], axis=1)
        fit = ols(self._weekly_factor("market"), X)
        if fit is None:
            notes.append("Too little overlapping history to link the market to these factors; market move assumed 0.")
            return {"move_pct": 0.0, "basis": "insufficient history", "links": []}
        links = [{"factor": f, "per_unit": round(fit.coef[f], 3), "t": round(fit.t[f], 2), "used": abs(fit.t[f]) >= T_MIN} for f in fs]
        move = sum(fit.use(x.factor) * x.change for x in shocks)
        return {"move_pct": round(move, 2), "basis": f"historical, {fit.n} weeks", "links": links}

    def _extrapolation(self, shocks: list[Shock], notes: list[str]) -> None:
        for x in shocks:
            w = self._weekly_factor(x.factor)
            if len(w):
                biggest = float(w.abs().max())
                if biggest and abs(x.change) > 3 * biggest:
                    notes.append(f"{FACTORS[x.factor][0]} {x.change:+g}{FACTORS[x.factor][1]} is {abs(x.change) / biggest:.0f}× the largest weekly move "
                                 f"in the sample ({biggest:.2f}{FACTORS[x.factor][1]}); linear estimates get less reliable that far out.")

    def _sector_fits(self, factors: list[str]) -> dict[str, dict[str, Any]]:
        mkt = self._weekly_factor("market")
        X = pd.concat([mkt] + [self._weekly_factor(f) for f in factors if f != "market"], axis=1)
        out = {}
        for sector, idx in SECTOR_INDEX.items():
            w = weekly(load_index(self.s, idx, self.as_of, self.lookback))
            fit = ols(w, X) if len(w) else None
            if fit is not None:
                out[sector] = {"index": idx, "fit": fit}
        return out

    def _map_rows(self, factors: list[str]) -> dict[tuple[str, str], m.SectorSensitivity]:
        codes = {MAP_FACTOR[f]: f for f in factors if f in MAP_FACTOR}
        if not codes:
            return {}
        rows = self.s.execute(select(m.SectorSensitivity, m.MacroFactor.code, m.Sector.name)
                              .join(m.MacroFactor, m.MacroFactor.id == m.SectorSensitivity.factor_id)
                              .join(m.Sector, m.Sector.id == m.SectorSensitivity.sector_id)
                              .where(m.SectorSensitivity.status == "approved", m.MacroFactor.code.in_(list(codes)))).all()
        return {(codes[code], sec): r for r, code, sec in rows}

    def _stock_beta(self, iid: int, cache: dict[int, Optional[float]]) -> Optional[float]:
        if iid not in cache:
            b = load_bars(self.s, [iid], self.as_of, self.lookback).get(iid)
            fit = ols(weekly(b["close"]), self._weekly_factor("market").to_frame()) if b is not None and len(b) else None
            cache[iid] = None if fit is None else 0.67 * fit.coef["market"] + 0.33     # Blume shrinkage towards 1
        return cache[iid]

    def run(self, shocks: list[Shock], portfolio_ids: Optional[list[int]] = None, name: str = "Custom scenario",
            notes: Optional[list[str]] = None) -> StressResult:
        notes = list(notes or [])
        if not shocks:
            raise ValueError("a scenario needs at least one shock")
        self._extrapolation(shocks, notes)
        market = self._market_move(shocks, notes)
        mkt_move = market["move_pct"]
        factors = [x.factor for x in shocks if x.factor != "market"]
        fits = self._sector_fits(factors)
        maps = self._map_rows(factors)
        sector_move: dict[str, float] = {}
        sectors = []
        for sector, v in sorted(fits.items()):
            fit: Fit = v["fit"]
            parts = [{"factor": "market", "per_unit": round(fit.coef["market"], 3), "t": round(fit.t["market"], 2),
                      "points": round(fit.coef["market"] * mkt_move, 2)}]
            move = fit.coef["market"] * mkt_move
            checks = []
            for x in shocks:
                if x.factor == "market":
                    continue
                own = fit.use(x.factor) * x.change
                move += own
                parts.append({"factor": x.factor, "per_unit": round(fit.coef[x.factor], 3), "t": round(fit.t[x.factor], 2),
                              "points": round(own, 2), "used": abs(fit.t[x.factor]) >= T_MIN})
                row = maps.get((x.factor, sector))
                if row is not None:
                    map_sign = int(row.direction) * (1 if x.change > 0 else -1)
                    hist = fit.use(x.factor) * x.change
                    checks.append({"factor": x.factor, "map_direction": map_sign, "map_magnitude": float(row.magnitude),
                                   "rationale": row.rationale,
                                   "agreement": "history inconclusive" if hist == 0 else ("agrees" if (hist > 0) == (map_sign > 0) else "disagrees")})
            sector_move[sector] = move
            sectors.append({"sector": sector, "index": v["index"], "move_pct": round(move, 2), "weeks": fit.n,
                            "parts": parts, "map": checks})
        sectors.sort(key=lambda r: r["move_pct"])

        def stock_move(iid: int, sector: Optional[str], betas: dict) -> tuple[float, str]:
            beta = self._stock_beta(iid, betas)
            own = 0.0
            if sector in fits:
                fit = fits[sector]["fit"]
                own = sum(fit.use(x.factor) * x.change for x in shocks if x.factor != "market")
            if beta is None:
                if sector in sector_move:
                    return sector_move[sector], "sector estimate (short stock history)"
                return mkt_move + own, "market estimate (no history)"
            return beta * mkt_move + own, f"beta {beta:.2f}" + ("" if sector in fits else " · no sector index")

        holds, ports = self.holdings(portfolio_ids)
        betas: dict[int, Optional[float]] = {}
        by_port: dict[int, list] = {pid: [] for pid in ports}
        for h in holds:
            mv, basis = stock_move(h.iid, h.sector, betas)
            price = h.last * (1 + mv / 100)
            by_port.setdefault(h.portfolio_id, []).append({
                "symbol": h.symbol, "sector": h.sector, "qty": h.qty, "last": round(h.last, 2), "value": round(h.value, 2),
                "move_pct": round(mv, 2), "pnl": round(h.value * mv / 100, 2), "est_price": round(price, 2), "basis": basis,
                "stop": h.stop, "breaches_stop": h.stop is not None and price <= h.stop,
                "target": h.target, "reaches_target": h.target is not None and price >= h.target})
        portfolios = []
        for pid, info in ports.items():
            rows = sorted(by_port.get(pid, []), key=lambda r: r["pnl"])
            invested = sum(r["value"] for r in rows)
            nav = info["cash"] + invested
            pnl = sum(r["pnl"] for r in rows)
            for r in rows:
                r["weight_pct"] = round(r["value"] / nav * 100, 2) if nav else None
            portfolios.append({**info, "nav": round(nav, 2), "invested": round(invested, 2), "pnl": round(pnl, 2),
                               "pnl_pct": round(pnl / nav * 100, 2) if nav else None,
                               "benchmark_pct": round(mkt_move * invested / nav, 2) if nav else None,
                               "stops_breached": sum(r["breaches_stop"] for r in rows), "holdings": rows})
        picks = []
        for p in self.open_picks():
            mv, basis = stock_move(p["iid"], p["sector"], betas)
            price = p["last"] * (1 + mv / 100)
            picks.append({k: v for k, v in p.items() if k != "iid"} | {
                "move_pct": round(mv, 2), "est_price": round(price, 2), "basis": basis,
                "breaches_stop": p["stop"] is not None and price <= p["stop"]})
        picks.sort(key=lambda r: r["move_pct"])
        return StressResult(
            as_of=self.as_of.isoformat(), scenario={"name": name, "shocks": [asdict(x) for x in shocks]}, market=market,
            sectors=sectors, portfolios=portfolios, open_picks=picks, notes=notes,
            assumptions=[f"Weekly history up to {self.as_of.isoformat()}; a link counts only when |t| ≥ {T_MIN}.",
                         "Linear: twice the shock gives twice the move. Real moves in a crisis are larger and more correlated.",
                         "Stocks follow their market beta (shrunk towards 1) plus their sector's beyond-market sensitivity; company news is ignored.",
                         "Estimates for one scenario, not a forecast. Stops are checked at the estimated price; gaps can fill worse."])

    # historical replay
    def worst_window(self, days: int) -> Optional[tuple[date, date, float]]:
        bench = load_index(self.s, BENCH, self.as_of, 4000)
        if len(bench) <= days:
            return None
        ret = bench / bench.shift(days) - 1
        end = ret.idxmin()
        i = bench.index.get_loc(end)
        return bench.index[i - days], end, float(ret.min() * 100)

    def replay(self, days: int = 20, start: Optional[date] = None, end: Optional[date] = None,
               portfolio_ids: Optional[list[int]] = None) -> dict[str, Any]:
        if start and end:
            bench = load_index(self.s, BENCH, end, (end - start).days + 10)
            b = bench[bench.index >= start]
            if len(b) < 2:
                raise ValueError("no benchmark data in that window")
            start, end, bret = b.index[0], b.index[-1], float(b.iloc[-1] / b.iloc[0] * 100 - 100)
        else:
            w = self.worst_window(days)
            if w is None:
                raise ValueError("not enough benchmark history for a replay")
            start, end, bret = w
        holds, ports = self.holdings(portfolio_ids)
        lookback = (self.as_of - start).days + 10
        bars = load_bars(self.s, sorted({h.iid for h in holds}), end, lookback)
        betas: dict[int, Optional[float]] = {}
        by_port: dict[int, list] = {pid: [] for pid in ports}
        for h in holds:
            b = bars.get(h.iid)
            c = b["close"] if b is not None else pd.Series(dtype=float)
            c = c[(c.index >= start) & (c.index <= end)]
            if len(c) >= 2 and c.index[0] <= start + timedelta(days=5):
                mv, basis = float(c.iloc[-1] / c.iloc[0] * 100 - 100), "actual"
            else:
                beta = self._stock_beta(h.iid, betas)
                mv, basis = (beta if beta is not None else 1.0) * bret, "beta estimate (not listed then)"
            price = h.last * (1 + mv / 100)
            by_port.setdefault(h.portfolio_id, []).append({
                "symbol": h.symbol, "sector": h.sector, "value": round(h.value, 2), "move_pct": round(mv, 2),
                "pnl": round(h.value * mv / 100, 2), "basis": basis, "stop": h.stop,
                "breaches_stop": h.stop is not None and price <= h.stop})
        portfolios = []
        for pid, info in ports.items():
            rows = sorted(by_port.get(pid, []), key=lambda r: r["pnl"])
            invested = sum(r["value"] for r in rows)
            nav = info["cash"] + invested
            pnl = sum(r["pnl"] for r in rows)
            portfolios.append({**info, "nav": round(nav, 2), "pnl": round(pnl, 2), "pnl_pct": round(pnl / nav * 100, 2) if nav else None,
                               "stops_breached": sum(r["breaches_stop"] for r in rows), "holdings": rows})
        sectors = []
        for sector, idx in SECTOR_INDEX.items():
            ser = load_index(self.s, idx, end, (end - start).days + 10)
            ser = ser[ser.index >= start]
            if len(ser) >= 2:
                sectors.append({"sector": sector, "move_pct": round(float(ser.iloc[-1] / ser.iloc[0] * 100 - 100), 2)})
        sectors.sort(key=lambda r: r["move_pct"])
        return {"as_of": self.as_of.isoformat(), "window": {"start": start.isoformat(), "end": end.isoformat(),
                "benchmark_pct": round(bret, 2), "trading_days": days}, "sectors": sectors, "portfolios": portfolios,
                "assumptions": ["Today's holdings, each moved by its own real return over the window (no rebalancing, no stops)."]}
