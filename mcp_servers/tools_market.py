"""market-mcp: instruments, prices, indices, corporate actions, calendar, breadth, data quality."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional

from sqlalchemy import or_, select

from core import models as m
from ingestion.quality import latest_gate
from ingestion.reference import universe_members
from quant.data import load_bars, load_index

from .registry import IST, MAX_PRICE_ROWS, Out, ToolFailure, ctx, decision_date, parse_date, tool


def _eod(d) -> datetime:
    return datetime.combine(d, time(15, 30), IST)


def _instrument(s, symbol: str) -> m.Instrument:
    inst = s.scalar(select(m.Instrument).where(m.Instrument.symbol == symbol.upper()))
    if inst is None:
        raise ToolFailure("NOT_FOUND", f"unknown symbol {symbol}")
    return inst


@tool("market")
def resolve_instrument(query: str, as_of: str = "") -> Out:
    """Find NSE instruments by symbol, company name or ISIN. Returns up to 5 candidates with a confidence."""
    q = query.strip().upper()
    with ctx().factory() as s:
        members = set(universe_members(s, ctx().settings.universe, decision_date(as_of)))
        rows = s.scalars(select(m.Instrument).where(or_(
            m.Instrument.symbol == q, m.Instrument.isin == q, m.Instrument.symbol.like(f"{q}%"),
            m.Instrument.name.ilike(f"%{query.strip()}%"))).limit(25)).all()
        out = []
        for r in rows:
            conf = 0.99 if r.symbol == q or r.isin == q else 0.85 if r.name.upper().startswith(q) else 0.6
            conf += 0.005 if r.id in members else 0
            sector = s.get(m.Sector, r.sector_id).name if r.sector_id else r.industry
            out.append({"symbol": r.symbol, "name": r.name, "isin": r.isin, "sector": sector,
                        "in_universe": r.id in members, "confidence": round(min(conf, 0.99), 3)})
    out.sort(key=lambda c: (-c["confidence"], not c["in_universe"], c["symbol"]))
    return Out({"query": query, "candidates": out[:5]}, "calc", "instrument_master")


@tool("market")
def get_price_history(symbol: str, from_date: str = "", to_date: str = "", adjusted: bool = True,
                      as_of: str = "") -> Out:
    """Daily OHLCV for an NSE symbol from the local store, split/bonus adjusted by default. At most 400 rows."""
    end = min(parse_date(to_date, decision_date(as_of)), decision_date(as_of))
    start = parse_date(from_date, end - timedelta(days=180))
    with ctx().factory() as s:
        inst = _instrument(s, symbol)
        bars = load_bars(s, [inst.id], end, lookback_days=(end - start).days).get(inst.id)
        if bars is None or bars.empty:
            raise ToolFailure("NOT_FOUND", f"no prices for {symbol} in {start}..{end}")
        if not adjusted:
            raw = {p.trade_date: p for p in s.scalars(select(m.PriceDaily).where(
                m.PriceDaily.instrument_id == inst.id, m.PriceDaily.trade_date >= start, m.PriceDaily.trade_date <= end))}
    bars = bars.tail(MAX_PRICE_ROWS)
    rows = []
    for d, r in bars.iterrows():
        if adjusted:
            rows.append({"date": d.isoformat(), "open": round(r.open, 2), "high": round(r.high, 2), "low": round(r.low, 2),
                         "close": round(r.close, 2), "volume": int(r.volume) if r.volume == r.volume else None})
        else:
            p = raw[d]
            rows.append({"date": d.isoformat(), "open": float(p.open), "high": float(p.high), "low": float(p.low),
                         "close": float(p.close), "volume": p.volume})
    last = bars.index[-1]
    return Out({"symbol": symbol.upper(), "adjusted": adjusted, "rows": rows,
                "hi52": round(float(bars["high"].tail(252).max()), 2), "lo52": round(float(bars["low"].tail(252).min()), 2)},
               "price_series", "nse_bhavcopy", as_of=_eod(last),
               data_quality="ok" if last >= decision_date(as_of) - timedelta(days=4) else "stale")


@tool("market")
def get_quote_snapshot(symbol: str, as_of: str = "") -> Out:
    """Last end-of-day close, previous close, volume, delivery % and 52-week range."""
    end = decision_date(as_of)
    with ctx().factory() as s:
        inst = _instrument(s, symbol)
        rows = s.scalars(select(m.PriceDaily).where(m.PriceDaily.instrument_id == inst.id, m.PriceDaily.trade_date <= end)
                         .order_by(m.PriceDaily.trade_date.desc()).limit(252)).all()
    if not rows:
        raise ToolFailure("NOT_FOUND", f"no prices for {symbol}")
    p = rows[0]
    adj = [float(r.adj_close or r.close) for r in rows]
    return Out({"symbol": inst.symbol, "date": p.trade_date.isoformat(), "close": float(p.close),
                "prev_close": float(p.prev_close) if p.prev_close else None, "volume": p.volume,
                "delivery_pct": float(p.delivery_pct) if p.delivery_pct is not None else None,
                "hi52_adj": max(adj), "lo52_adj": min(adj)}, "price_series", p.source, as_of=_eod(p.trade_date))


@tool("market")
def get_index_history(index_code: str, from_date: str = "", to_date: str = "", as_of: str = "") -> Out:
    """Index closes, e.g. NIFTY 50, NIFTY 500, INDIA VIX, NIFTY IT. index_code SECTOR_INDICES returns 1m/3m returns of all NSE sector indices."""
    end = min(parse_date(to_date, decision_date(as_of)), decision_date(as_of))
    start = parse_date(from_date, end - timedelta(days=180))
    with ctx().factory() as s:
        if index_code.upper() == "SECTOR_INDICES":
            from quant.engine import SECTOR_INDEX

            out = []
            for sector, code in sorted(SECTOR_INDEX.items()):
                ser = load_index(s, code, end, 130)
                if len(ser) < 22:
                    continue
                r1 = ser.iloc[-1] / ser.iloc[-22] - 1
                r3 = ser.iloc[-1] / ser.iloc[-64] - 1 if len(ser) >= 64 else None
                out.append({"sector": sector, "index": code, "close": round(float(ser.iloc[-1]), 2),
                            "ret_1m_pct": round(r1 * 100, 2), "ret_3m_pct": round(r3 * 100, 2) if r3 is not None else None})
            if not out:
                raise ToolFailure("NOT_FOUND", "no sector index history")
            return Out({"sectors": out}, "price_series", "nse_index_bhavcopy", as_of=_eod(end))
        ser = load_index(s, index_code.upper(), end, (end - start).days)
    if ser.empty:
        raise ToolFailure("NOT_FOUND", f"no history for index {index_code}")
    ser = ser.tail(MAX_PRICE_ROWS)
    return Out({"index_code": index_code.upper(), "rows": [{"date": d.isoformat(), "close": round(v, 2)} for d, v in ser.items()],
                "ret_1m_pct": round((ser.iloc[-1] / ser.iloc[-22] - 1) * 100, 2) if len(ser) >= 22 else None},
               "price_series", "nse_index_bhavcopy", as_of=_eod(ser.index[-1]))


@tool("market")
def get_universe(universe: str = "", as_of: str = "") -> Out:
    """Point-in-time constituents of NIFTY50/NIFTY200/NIFTY500 with sector."""
    name = universe or ctx().settings.universe
    with ctx().factory() as s:
        members = universe_members(s, name, decision_date(as_of))
        out = []
        for iid, sym in sorted(members.items(), key=lambda x: x[1]):
            inst = s.get(m.Instrument, iid)
            out.append({"symbol": sym, "name": inst.name, "sector": inst.industry})
    return Out({"universe": name, "members": out}, "calc", "niftyindices")


@tool("market")
def get_corporate_actions(symbol: str = "", from_date: str = "", to_date: str = "", types: Optional[list[str]] = None,
                          as_of: str = "") -> Out:
    """Splits, bonuses, dividends, rights, buybacks and (de)mergers with ex-dates, optionally for one symbol."""
    d = decision_date(as_of)
    start, end = parse_date(from_date, d - timedelta(days=30)), parse_date(to_date, d + timedelta(days=60))
    with ctx().factory() as s:
        q = select(m.CorporateAction, m.Instrument.symbol).join(m.Instrument).where(
            m.CorporateAction.ex_date >= start, m.CorporateAction.ex_date <= end)
        if symbol:
            q = q.where(m.Instrument.symbol == symbol.upper())
        if types:
            q = q.where(m.CorporateAction.action_type.in_(types))
        rows = s.execute(q.order_by(m.CorporateAction.ex_date).limit(100)).all()
    items = [{"symbol": sym, "action": a.action_type, "ex_date": a.ex_date.isoformat(),
              "ratio": f"{a.ratio_num}:{a.ratio_den}" if a.ratio_num else None,
              "amount_per_share": float(a.amount_per_share) if a.amount_per_share is not None else None,
              "subject": (a.details or {}).get("subject")} for a, sym in rows]
    return Out({"symbol": symbol.upper() or None, "items": items}, "event", "nse_corp_actions")


@tool("market")
def get_market_breadth(as_of: str = "") -> Out:
    """Advances/declines, % above 50/200-DMA and new 52-week highs/lows across the universe."""
    return Out(ctx().quant.market_breadth(decision_date(as_of)), "indicator", "quant_engine")


@tool("market")
def get_trading_calendar(from_date: str = "", to_date: str = "", as_of: str = "") -> Out:
    """NSE trading days and holidays in a date range."""
    d = decision_date(as_of)
    start, end = parse_date(from_date, d), parse_date(to_date, d + timedelta(days=30))
    with ctx().factory() as s:
        rows = s.scalars(select(m.TradingCalendar).where(m.TradingCalendar.cal_date >= start,
                                                         m.TradingCalendar.cal_date <= end)).all()
    return Out({"holidays": [{"date": r.cal_date.isoformat(), "note": r.note} for r in rows
                             if not r.is_trading_day and r.note != "weekend"],
                "trading_days": sum(r.is_trading_day for r in rows)}, "calc", "trading_calendar")


@tool("market")
def data_quality_status(as_of: str = "") -> Out:
    """Result of the data-quality gate for the decision date: passed, trading day, issues, excluded symbols."""
    d = decision_date(as_of)
    with ctx().factory() as s:
        g = latest_gate(s, d)
    if g is None:
        return Out({"passed": False, "is_trading_day": True, "issues": [f"gate has not run for {d}"], "excluded": []},
                   "calc", "data_gate")
    return Out(g.as_tool_payload(), "calc", "data_gate")
