"""Synthetic, deterministic market database for tests (not market data)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np

from core import models as m
from core.db import make_engine, session_factory
from ingestion.reference import seed, sync_calendar, sync_universe

AS_OF = date(2026, 10, 12)
IST = timezone(timedelta(hours=5, minutes=30))
DECISION = datetime(2026, 10, 12, 18, 30, tzinfo=IST)


def trading_days(n: int, end: date = AS_OF) -> list[date]:
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def synthetic_db(n_up=10, n_flat=10, n_down=10, days=300, gate_passed=True, with_sector_indices=True,
                 url="sqlite:///:memory:"):
    engine = make_engine(url)
    m.Base.metadata.create_all(engine)
    f = session_factory(engine)
    rng = np.random.default_rng(7)
    dates = trading_days(days)
    names = [f"UP{i}" for i in range(n_up)] + [f"FLAT{i}" for i in range(n_flat)] + [f"DN{i}" for i in range(n_down)]
    industry = lambda sym: "Information Technology" if sym.startswith("UP") else "Financial Services"
    with f() as s, s.begin():
        seed(s)
        sync_calendar(s, dates[0], AS_OF + timedelta(days=60), [{"date": date(2026, 11, 9), "note": "Diwali"}])
        sync_universe(s, "NIFTY200", [{"symbol": x, "name": f"{x} Industries Ltd", "industry": industry(x),
                                       "series": "EQ", "isin": f"INE{i:06d}"} for i, x in enumerate(names)], dates[0])
        iids = {sym: s.scalar(m.Instrument.__table__.select().with_only_columns(m.Instrument.id)
                              .where(m.Instrument.symbol == sym)) for sym in names}
        for sym in names:
            drift = 0.003 if sym.startswith("UP") else -0.003 if sym.startswith("DN") else 0.0
            px = 500 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, days)))
            prev = None
            for d, p in zip(dates, px):
                p = Decimal(str(round(p, 2)))
                s.add(m.PriceDaily(instrument_id=iids[sym], trade_date=d, open=p, high=p * Decimal("1.01"),
                                   low=p * Decimal("0.99"), close=p, adj_close=p, prev_close=prev or p,
                                   volume=1_000_000, traded_value=p * 1_000_000, delivery_pct=Decimal("45"),
                                   source="nse_bhavcopy"))
                prev = p
        codes = ["NIFTY 500"] + (["NIFTY IT", "NIFTY FINANCIAL SERVICES"] if with_sector_indices else [])
        for code in codes:
            idx = 20000 * np.exp(np.cumsum(0.0005 + rng.normal(0, 0.005, days)))
            for d, v in zip(dates, idx):
                s.add(m.IndexPriceDaily(index_code=code, trade_date=d, close=Decimal(str(round(v, 2)))))
        for d in dates[-10:]:
            s.add(m.IndexPriceDaily(index_code="INDIA VIX", trade_date=d, close=Decimal("13.5")))
        s.add(m.CorporateAction(instrument_id=iids["UP0"], action_type="dividend", ex_date=AS_OF + timedelta(days=10),
                                amount_per_share=Decimal("5"), details={"subject": "Dividend - Rs 5 Per Share"},
                                source="nse_corp_actions"))
        s.add(m.DataGateResult(decision_date=AS_OF, passed=gate_passed, is_trading_day=True,
                               issues=[] if gate_passed else ["freshness 80% < 98%"], excluded=[], stats={}))
    return engine, f


def rss(items: list[tuple[str, str, datetime]]) -> str:
    """A Google News style RSS document: (title, publisher, published) tuples."""
    body = "".join(
        f"<item><title>{t} - {src}</title><link>https://news.example/{abs(hash(t))}</link>"
        f"<pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>"
        f"<description>&lt;a href=\"x\"&gt;{t}&lt;/a&gt;</description><source url=\"https://{src}\">{src}</source></item>"
        for t, src, pub in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'
