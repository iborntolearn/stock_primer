"""Paper-trading engine (§4.6).

Fill model: orders placed on day D fill at the open of the next trading day
(never on the signal bar), with slippage scaled by size vs ADV, a liquidity cap,
position/sector caps and a cash check including costs. Price-locked circuit days
leave the order pending (retried, then expired).

Daily EOD procedure per portfolio (idempotent per date):
  1. corporate actions with ex_date = today (split/bonus adjust qty, cost, stop, target; dividends to cash)
  2. fill pending orders at today's open
  3. exits: stop -> target -> horizon expiry -> thesis break (gaps fill at the open)
  4. mark to market at the close; write daily_snapshot with benchmark NAV
Fills and exits use actual (unadjusted) traded prices because positions hold real share counts.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m
from ingestion.adapters.corporate_actions import share_multiplier
from ingestion.reference import is_trading_day

from .costs import rates_for, slippage_bps, trade_costs

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_RULES: dict[str, Any] = {
    "max_position_pct": 10.0, "max_sector_pct": 30.0, "risk_per_trade_pct": 0.75, "adv_cap_pct": 3.0,
    "max_positions": 10, "min_cash_pct": 2.0, "max_fill_attempts": 3,
    "holding_days": {"short": 28, "medium": 180, "long": 1095},
}
BENCHMARK_INDEX = {"NIFTY 500": "NIFTY 500", "NIFTY500": "NIFTY 500", "NIFTY 50": "NIFTY 50", "NIFTY50": "NIFTY 50"}


class OrderRejected(Exception):
    pass


def D(x: float, q: str = "0.0001") -> Decimal:
    return Decimal(str(x)).quantize(Decimal(q))


def ist_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


@dataclass
class EodReport:
    portfolio_id: int
    day: date
    skipped: bool = False
    filled: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    corporate_actions: list[str] = field(default_factory=list)
    nav: Optional[float] = None


class Simulator:
    def __init__(self, factory: sessionmaker[Session]):
        self.f = factory

    # -- portfolios and orders -------------------------------------------------------
    def create_portfolio(self, name: str, starting_cash: float, type: str = "manual", benchmark: str = "NIFTY 500",
                         rules: Optional[dict] = None) -> int:
        with self.f() as s, s.begin():
            p = m.Portfolio(name=name, type=type, starting_cash=D(starting_cash, "0.01"), cash=D(starting_cash, "0.01"),
                            benchmark=benchmark, rules={**DEFAULT_RULES, **(rules or {})})
            s.add(p)
            s.flush()
            return p.id

    def place_order(self, portfolio_id: int, symbol: str, *, amount: Optional[float] = None, qty: Optional[int] = None,
                    stop: Optional[float] = None, target: Optional[float] = None, horizon: Optional[str] = None,
                    reco_id: Optional[int] = None, idempotency_key: Optional[str] = None,
                    placed_at: Optional[datetime] = None) -> dict[str, Any]:
        if (amount is None) == (qty is None):
            raise OrderRejected("give exactly one of amount or qty")
        if (amount is not None and amount <= 0) or (qty is not None and qty <= 0):
            raise OrderRejected("amount/qty must be positive")
        if stop is not None and target is not None and stop >= target:
            raise OrderRejected("stop must be below target")
        with self.f() as s, s.begin():
            if idempotency_key:
                existing = s.scalar(select(m.PaperOrder).where(m.PaperOrder.idempotency_key == idempotency_key))
                if existing:
                    return self._order_dict(s, existing)
            p = s.get(m.Portfolio, portfolio_id)
            if p is None or p.archived:
                raise OrderRejected(f"portfolio {portfolio_id} not found")
            inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == symbol.upper()))
            if inst is None:
                raise OrderRejected(f"unknown symbol {symbol}")
            o = m.PaperOrder(portfolio_id=portfolio_id, instrument_id=inst.id, reco_id=reco_id, side="BUY", qty=qty,
                             amount=D(amount, "0.01") if amount is not None else None,
                             stop=D(stop) if stop is not None else None, target=D(target) if target is not None else None,
                             horizon=horizon, placed_at=placed_at or datetime.now(timezone.utc),
                             idempotency_key=idempotency_key)
            s.add(o)
            s.flush()
            return self._order_dict(s, o)

    def _order_dict(self, s: Session, o: m.PaperOrder) -> dict[str, Any]:
        nxt = ist_date(o.placed_at) + timedelta(days=1)
        for _ in range(10):
            if is_trading_day(s, nxt):
                break
            nxt += timedelta(days=1)
        return {"order_id": o.id, "portfolio_id": o.portfolio_id, "symbol": s.get(m.Instrument, o.instrument_id).symbol,
                "status": o.status, "qty": o.qty, "amount": float(o.amount) if o.amount is not None else None,
                "stop": float(o.stop) if o.stop is not None else None,
                "target": float(o.target) if o.target is not None else None, "reco_id": o.reco_id,
                "expected_fill": f"{nxt.isoformat()} open", "reject_reason": o.reject_reason}

    # -- EOD -----------------------------------------------------------------------
    def run_eod(self, day: date) -> list[EodReport]:
        with self.f() as s:
            ids = list(s.scalars(select(m.Portfolio.id).where(m.Portfolio.archived.is_(False))))
            trading = is_trading_day(s, day)
        if not trading:
            return []
        return [self.run_eod_portfolio(pid, day) for pid in ids]

    def run_eod_portfolio(self, pid: int, day: date) -> EodReport:
        rep = EodReport(pid, day)
        with self.f() as s, s.begin():
            if s.get(m.DailySnapshot, (pid, day)) is not None:
                rep.skipped = True
                return rep
            p = s.get(m.Portfolio, pid)
            rules = {**DEFAULT_RULES, **(p.rules or {})}
            rates = rates_for(s, day)
            self._corporate_actions(s, p, day, rep)
            self._fill_orders(s, p, day, rules, rates, rep)
            self._exits(s, p, day, rules, rates, rep)
            rep.nav = self._snapshot(s, p, day)
        return rep

    @staticmethod
    def _bar(s: Session, iid: int, day: date) -> Optional[m.PriceDaily]:
        return s.get(m.PriceDaily, (iid, day))

    @staticmethod
    def _last_close(s: Session, iid: int, day: date) -> Optional[float]:
        c = s.scalar(select(m.PriceDaily.close).where(m.PriceDaily.instrument_id == iid, m.PriceDaily.trade_date <= day)
                     .order_by(m.PriceDaily.trade_date.desc()).limit(1))
        return float(c) if c is not None else None

    @staticmethod
    def _adv(s: Session, iid: int, day: date) -> tuple[float, float]:
        rows = s.execute(select(m.PriceDaily.volume, m.PriceDaily.traded_value).where(
            m.PriceDaily.instrument_id == iid, m.PriceDaily.trade_date < day)
            .order_by(m.PriceDaily.trade_date.desc()).limit(20)).all()
        if not rows:
            return 0.0, 0.0
        return (sum(float(v or 0) for v, _ in rows) / len(rows), sum(float(t or 0) for _, t in rows) / len(rows))

    def _nav(self, s: Session, p: m.Portfolio, day: date) -> float:
        hold = sum(pos.qty * (self._last_close(s, pos.instrument_id, day) or float(pos.avg_cost))
                   for pos in self._open_positions(s, p.id))
        return float(p.cash) + hold

    @staticmethod
    def _open_positions(s: Session, pid: int) -> list[m.Position]:
        return list(s.scalars(select(m.Position).where(m.Position.portfolio_id == pid, m.Position.status == "open")))

    def _corporate_actions(self, s: Session, p: m.Portfolio, day: date, rep: EodReport) -> None:
        for pos in self._open_positions(s, p.id):
            for ca in s.scalars(select(m.CorporateAction).where(m.CorporateAction.instrument_id == pos.instrument_id,
                                                                m.CorporateAction.ex_date == day)):
                sym = s.get(m.Instrument, pos.instrument_id).symbol
                if ca.action_type in ("split", "bonus"):
                    mult = share_multiplier(ca.action_type, ca.ratio_num, ca.ratio_den)
                    pos.qty = int(math.floor(pos.qty * mult))
                    pos.avg_cost = (pos.avg_cost / mult).quantize(Decimal("0.0001"))
                    if pos.stop is not None:
                        pos.stop = (pos.stop / mult).quantize(Decimal("0.0001"))
                    if pos.target is not None:
                        pos.target = (pos.target / mult).quantize(Decimal("0.0001"))
                    rep.corporate_actions.append(f"{sym} {ca.action_type} x{mult}")
                elif ca.action_type == "dividend" and ca.amount_per_share:
                    credit = pos.qty * ca.amount_per_share
                    p.cash += credit.quantize(Decimal("0.01"))
                    rep.corporate_actions.append(f"{sym} dividend {float(credit):.2f}")
                elif ca.action_type in ("demerger", "merger"):
                    rep.corporate_actions.append(f"{sym} {ca.action_type}: needs manual review")

    def _fill_orders(self, s: Session, p: m.Portfolio, day: date, rules: dict, rates: dict, rep: EodReport) -> None:
        orders = s.scalars(select(m.PaperOrder).where(m.PaperOrder.portfolio_id == p.id, m.PaperOrder.status == "pending")
                           .order_by(m.PaperOrder.id)).all()
        for o in orders:
            if ist_date(o.placed_at) >= day:
                continue                                   # never fill on the signal bar
            inst = s.get(m.Instrument, o.instrument_id)
            bar = self._bar(s, o.instrument_id, day)
            locked = bar is not None and bar.high == bar.low and (bar.volume or 0) < 1000
            if bar is None or bar.open is None or locked:
                o.attempts += 1
                if o.attempts >= rules["max_fill_attempts"]:
                    o.status, o.reject_reason = "expired", "no tradable session (missing bar or circuit lock)"
                    rep.rejected.append(f"{inst.symbol}: {o.reject_reason}")
                continue
            reason = self._try_fill(s, p, o, inst, bar, day, rules, rates)
            if reason:
                o.status, o.reject_reason = "rejected", reason
                rep.rejected.append(f"{inst.symbol}: {reason}")
            else:
                rep.filled.append(inst.symbol)

    def _try_fill(self, s, p, o, inst, bar, day, rules, rates) -> Optional[str]:
        open_px = float(bar.open)
        if o.stop is not None and open_px <= float(o.stop):
            return f"opened at {open_px} at or below stop {float(o.stop)}"
        nav = self._nav(s, p, day)
        adv_vol, adv_val = self._adv(s, o.instrument_id, day)
        want_value = float(o.amount) if o.amount is not None else o.qty * open_px
        bps = slippage_bps(want_value, adv_val, rates)
        price = float(D(open_px * (1 + bps / 1e4)))   # round first so cash and the trade row agree exactly
        qty = o.qty if o.qty is not None else int(want_value // price)
        caps = [qty]
        if adv_vol:
            caps.append(int(adv_vol * rules["adv_cap_pct"] / 100))
        caps.append(int(nav * rules["max_position_pct"] / 100 // price))
        sector_id = inst.sector_id
        if sector_id is not None:
            held = sum(pos.qty * (self._last_close(s, pos.instrument_id, day) or float(pos.avg_cost))
                       for pos in self._open_positions(s, p.id)
                       if s.get(m.Instrument, pos.instrument_id).sector_id == sector_id)
            caps.append(int(max(0.0, nav * rules["max_sector_pct"] / 100 - held) // price))
        qty = max(0, min(caps))
        while qty > 0 and qty * price + trade_costs("BUY", qty, price, rates)["total"] > float(p.cash):
            qty -= max(1, qty // 50)
        if qty <= 0:
            return "insufficient cash or capped to zero (liquidity/position/sector limits)"
        costs = trade_costs("BUY", qty, price, rates)
        t = m.PaperTrade(portfolio_id=p.id, instrument_id=inst.id, reco_id=o.reco_id, side="BUY", qty=qty,
                         fill_price=D(price), fill_date=day, brokerage=D(costs["brokerage"]), stt=D(costs["stt"]),
                         exch_charges=D(costs["exch_charges"]), gst=D(costs["gst"]), stamp_duty=D(costs["stamp_duty"]),
                         dp_charges=D(costs["dp_charges"]), slippage=D((price - open_px) * qty),
                         total_cost=D(costs["total"]), reason="manual" if p.type == "manual" else "auto_follow",
                         idempotency_key=f"order:{o.id}")
        s.add(t)
        s.flush()
        p.cash -= D(qty * price + costs["total"], "0.01")
        hold = rules["holding_days"].get(o.horizon or "short", 28)
        existing = s.get(m.Position, (p.id, inst.id, day))
        if existing:
            total = existing.qty + qty
            existing.avg_cost = D((float(existing.avg_cost) * existing.qty + price * qty) / total)
            existing.qty = total
        else:
            s.add(m.Position(portfolio_id=p.id, instrument_id=inst.id, opened_on=day, reco_id=o.reco_id,
                             horizon=o.horizon, qty=qty, avg_cost=D(price), stop=o.stop, target=o.target,
                             expires_on=day + timedelta(days=hold), status="open"))
        o.status, o.fill_trade_id, o.qty = "filled", t.id, qty
        return None

    def _exits(self, s: Session, p: m.Portfolio, day: date, rules: dict, rates: dict, rep: EodReport) -> None:
        for pos in self._open_positions(s, p.id):
            bar = self._bar(s, pos.instrument_id, day)
            if bar is None or bar.close is None:
                continue
            o, h, l, c = (float(bar.open), float(bar.high), float(bar.low), float(bar.close))
            exit_px, reason = None, None
            stop = float(pos.stop) if pos.stop is not None else None
            target = float(pos.target) if pos.target is not None else None
            if target is not None and o >= target:
                exit_px, reason = o, "target"                       # gap through target: better fill at open
            elif stop is not None and l <= stop:
                exit_px, reason = (o if o <= stop else stop), "stop"   # gap through stop: worse fill at open
            elif target is not None and h >= target:
                exit_px, reason = target, "target"
            elif pos.expires_on is not None and pos.expires_on <= day:
                exit_px, reason = c, "horizon_end"
            elif pos.reco_id:
                r = s.get(m.Recommendation, pos.reco_id)
                if r is not None and r.status == "invalidated":
                    exit_px, reason = c, "thesis_break"
            if exit_px is None:
                continue
            _, adv_val = self._adv(s, pos.instrument_id, day)
            price = float(D(exit_px * (1 - slippage_bps(pos.qty * exit_px, adv_val, rates) / 1e4)))
            costs = trade_costs("SELL", pos.qty, price, rates)
            s.add(m.PaperTrade(portfolio_id=p.id, instrument_id=pos.instrument_id, reco_id=pos.reco_id, side="SELL",
                               qty=pos.qty, fill_price=D(price), fill_date=day, brokerage=D(costs["brokerage"]),
                               stt=D(costs["stt"]), exch_charges=D(costs["exch_charges"]), gst=D(costs["gst"]),
                               stamp_duty=D(0), dp_charges=D(costs["dp_charges"]),
                               slippage=D((exit_px - price) * pos.qty), total_cost=D(costs["total"]), reason=reason,
                               idempotency_key=f"exit:{p.id}:{pos.instrument_id}:{pos.opened_on}:{day}"))
            p.cash += D(pos.qty * price - costs["total"], "0.01")
            pos.status = "closed"
            rep.exits.append(f"{s.get(m.Instrument, pos.instrument_id).symbol} {reason} @ {price:.2f}")

    def _snapshot(self, s: Session, p: m.Portfolio, day: date) -> float:
        positions = self._open_positions(s, p.id)
        by_sector: dict[str, float] = {}
        by_h: dict[str, float] = {}
        hold = 0.0
        for pos in positions:
            v = pos.qty * (self._last_close(s, pos.instrument_id, day) or float(pos.avg_cost))
            hold += v
            inst = s.get(m.Instrument, pos.instrument_id)
            sec = s.get(m.Sector, inst.sector_id).name if inst.sector_id else (inst.industry or "Unknown")
            by_sector[sec] = by_sector.get(sec, 0) + v
            by_h[pos.horizon or "?"] = by_h.get(pos.horizon or "?", 0) + v
        nav = float(p.cash) + hold
        bench = None
        code = BENCHMARK_INDEX.get(p.benchmark, p.benchmark)
        idx = s.scalar(select(m.IndexPriceDaily.close).where(m.IndexPriceDaily.index_code == code,
                                                             m.IndexPriceDaily.trade_date <= day)
                       .order_by(m.IndexPriceDaily.trade_date.desc()).limit(1))
        rules = dict(p.rules or {})
        if idx is not None:
            if "benchmark_base" not in rules:
                rules["benchmark_base"] = float(idx)
                p.rules = rules
            bench = float(p.starting_cash) * float(idx) / rules["benchmark_base"]
        prev = s.scalar(select(m.DailySnapshot.nav).where(m.DailySnapshot.portfolio_id == p.id,
                                                          m.DailySnapshot.snap_date < day)
                        .order_by(m.DailySnapshot.snap_date.desc()).limit(1))
        s.add(m.DailySnapshot(portfolio_id=p.id, snap_date=day, cash=D(float(p.cash), "0.01"), holdings_value=D(hold, "0.01"),
                              nav=D(nav, "0.01"), benchmark_nav=D(bench, "0.01") if bench is not None else None,
                              day_pnl=D(nav - float(prev if prev is not None else p.starting_cash), "0.01"),
                              exposure={"by_sector": {k: round(v / nav, 4) for k, v in by_sector.items()} if nav else {},
                                        "by_horizon": {k: round(v / nav, 4) for k, v in by_h.items()} if nav else {},
                                        "positions": len(positions)}))
        return round(nav, 2)

    # -- auto-follow (feature D3) ----------------------------------------------------
    def sync_auto_follow(self, as_of: datetime) -> list[dict[str, Any]]:
        """Order every published daily-run recommendation not yet followed. No discretion, so the
        auto-follow portfolio stays a clean scorecard of the recommender."""
        placed = []
        with self.f() as s:
            pids = list(s.scalars(select(m.Portfolio.id).where(m.Portfolio.type == "auto_follow",
                                                               m.Portfolio.archived.is_(False))))
            recos = s.scalars(select(m.Recommendation).where(m.Recommendation.status == "published",
                                                             m.Recommendation.published_at <= as_of)
                              .order_by(m.Recommendation.published_at)).all()
            recos = [r for r in recos if (r.meta or {}).get("run_type", "daily") == "daily"]
        for pid in pids:
            for r in recos:
                with self.f() as s:
                    if s.scalar(select(m.PaperOrder.id).where(m.PaperOrder.portfolio_id == pid,
                                                              m.PaperOrder.reco_id == r.id)) is not None:
                        continue
                    p = s.get(m.Portfolio, pid)
                    rules = {**DEFAULT_RULES, **(p.rules or {})}
                    open_n = len(self._open_positions(s, pid)) + s.scalar(
                        select(func.count()).select_from(m.PaperOrder).where(m.PaperOrder.portfolio_id == pid,
                                                                             m.PaperOrder.status == "pending"))
                    nav = self._nav(s, p, ist_date(as_of))
                    amount = nav * min(rules["max_position_pct"], 100 / rules["max_positions"]) / 100
                    sym = s.get(m.Instrument, r.instrument_id).symbol
                    reserved = sum(float(o.amount or 0) for o in s.scalars(select(m.PaperOrder).where(
                        m.PaperOrder.portfolio_id == pid, m.PaperOrder.status == "pending")))
                    free_cash = float(p.cash) - reserved - nav * rules["min_cash_pct"] / 100
                if open_n >= rules["max_positions"] or free_cash < amount * 0.5:
                    continue
                placed.append(self.place_order(
                    pid, sym, amount=min(amount, free_cash), stop=float(r.stop) if r.stop else None,
                    target=float(r.target) if r.target else None, horizon=r.horizon, reco_id=r.id,
                    idempotency_key=f"auto:{pid}:{r.id}", placed_at=r.published_at))
        return placed

    # -- quant-only shadow (feature: agents-vs-quant value-add test) ---------------------------
    def sync_quant_shadow(self, as_of: datetime, picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Order the quant engine's own top picks (no agents, no critic) into each quant_shadow portfolio.
        Same sizing and limits as auto-follow; a stock already held or pending is skipped."""
        placed = []
        day = ist_date(as_of)
        with self.f() as s:
            pids = list(s.scalars(select(m.Portfolio.id).where(m.Portfolio.type == "quant_shadow",
                                                               m.Portfolio.archived.is_(False))))
        for pid in pids:
            for pk in picks:
                with self.f() as s:
                    p = s.get(m.Portfolio, pid)
                    rules = {**DEFAULT_RULES, **(p.rules or {})}
                    inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == pk["symbol"]))
                    if inst is None:
                        continue
                    held = s.scalar(select(m.Position.instrument_id).where(
                        m.Position.portfolio_id == pid, m.Position.instrument_id == inst.id, m.Position.status == "open"))
                    pending = s.scalar(select(m.PaperOrder.id).where(
                        m.PaperOrder.portfolio_id == pid, m.PaperOrder.instrument_id == inst.id, m.PaperOrder.status == "pending"))
                    if held is not None or pending is not None:
                        continue
                    open_n = len(self._open_positions(s, pid)) + s.scalar(
                        select(func.count()).select_from(m.PaperOrder).where(m.PaperOrder.portfolio_id == pid,
                                                                             m.PaperOrder.status == "pending"))
                    nav = self._nav(s, p, day)
                    amount = nav * min(rules["max_position_pct"], 100 / rules["max_positions"]) / 100
                    reserved = sum(float(o.amount or 0) for o in s.scalars(select(m.PaperOrder).where(
                        m.PaperOrder.portfolio_id == pid, m.PaperOrder.status == "pending")))
                    free_cash = float(p.cash) - reserved - nav * rules["min_cash_pct"] / 100
                if open_n >= rules["max_positions"] or free_cash < amount * 0.5:
                    continue
                placed.append(self.place_order(
                    pid, pk["symbol"], amount=min(amount, free_cash), stop=pk.get("stop"), target=pk.get("target"),
                    horizon=pk.get("horizon", "short"), idempotency_key=f"shadow:{pid}:{day.isoformat()}:{pk['symbol']}",
                    placed_at=as_of))
        return placed

    def ensure_standard_portfolios(self, starting_cash: float = 1_000_000) -> dict[str, int]:
        """The auto-follow scorecard and its quant-only control, created once."""
        out = {}
        with self.f() as s:
            existing = {p.type: p.id for p in s.scalars(select(m.Portfolio).where(m.Portfolio.archived.is_(False)))}
        for typ, name in (("auto_follow", "Auto-follow"), ("quant_shadow", "Quant-only shadow")):
            out[typ] = existing.get(typ) or self.create_portfolio(name, starting_cash, type=typ)
        return out
