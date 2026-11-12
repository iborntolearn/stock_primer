"""Ingestion: parsers against recorded NSE files, EOD load, adjustment, reconciliation, fallback, gate."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from core import models as m
from core.db import make_engine, session_factory
from core.settings import Settings
from ingestion.adapters.base import DiskCache, RetryableError, TokenBucket
from ingestion.adapters.corporate_actions import NseCorporateActions, parse_subject, share_multiplier
from ingestion.adapters.nse import (
    NseArchives, parse_equity_bhavcopy, parse_full_bhavcopy, parse_holidays, parse_index_bhavcopy,
)
from ingestion.adapters.universe import parse_constituents
from ingestion.adapters.yahoo import YahooFinance
from ingestion.eod import recompute_adjusted, run_eod_ingest
from ingestion.quality import run_gate
from ingestion.reference import seed, sync_calendar, sync_universe, universe_members

FX = Path(__file__).parent / "fixtures" / "nse"
DAY = date(2026, 10, 12)
PREV = date(2026, 10, 9)
SYMBOLS = ["HDFCBANK", "INFY", "ITC", "RELIANCE", "SBIN", "TCS"]
FAST = TokenBucket(rate=1000, capacity=1000)
IST = timezone(timedelta(hours=5, minutes=30))


def udiff() -> str:
    return (FX / "udiff_2026-10-12.csv").read_text()


# -- parsers ---------------------------------------------------------------------
def test_parse_udiff_keeps_equity_series_only():
    rows = parse_equity_bhavcopy(udiff())
    assert {r["symbol"] for r in rows} == set(SYMBOLS) | {"ABINFRA"}   # gold bond (GB) dropped, BE kept
    r = next(r for r in rows if r["symbol"] == "HDFCBANK")
    assert r["trade_date"] == DAY and isinstance(r["close"], Decimal) and r["volume"] > 0
    assert r["low"] <= r["close"] <= r["high"]


def test_parse_legacy_bhavcopy():
    text = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,\n"
            "INFY,EQ,1500,1510,1490,1505,1504,1498,100000,150500000,05-JUL-2024,5000,INE009A01021,\n")
    (r,) = parse_equity_bhavcopy(text)
    assert r["trade_date"] == date(2024, 7, 5) and r["close"] == Decimal("1505")


def test_parse_full_bhavcopy_delivery():
    d = parse_full_bhavcopy((FX / "full_2026-10-12.csv").read_text())
    assert set(d) == set(SYMBOLS) and all(Decimal(0) < v <= 100 for v in d.values())


def test_parse_index_bhavcopy():
    rows = {r["index_code"]: r for r in parse_index_bhavcopy((FX / "index_2026-10-12.csv").read_text())}
    assert {"NIFTY 50", "NIFTY 200", "NIFTY 500", "INDIA VIX", "NIFTY AUTO"} <= set(rows)
    assert rows["NIFTY 50"]["trade_date"] == DAY


def test_parse_holidays():
    hol = parse_holidays({"CM": [{"tradingDate": "26-Jan-2026", "description": "Republic Day"}], "FO": []})
    assert hol == [{"date": date(2026, 1, 26), "note": "Republic Day"}]


@pytest.mark.parametrize("subject,expected", [
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", ("split", 10, 2)),
    ("Bonus 1:1", ("bonus", 1, 1)),
    ("Rights 3:8 @ Premium Rs 14/-", ("rights", 3, 8)),
    ("Scheme Of Arrangement - Bonus Ncrps 46:1", ("other", None, None)),
    ("Buy Back", ("buyback", None, None)),
    ("Demerger", ("demerger", None, None)),
])
def test_parse_corporate_action_subjects(subject, expected):
    (a,) = parse_subject(subject)
    assert (a["action_type"], a.get("ratio_num"), a.get("ratio_den")) == expected


def test_dividend_amounts_are_summed():
    (a,) = parse_subject("Interim Dividend - Rs 7.50 Per Share & Special Dividend Rs 86.50 Per Share")
    assert a["action_type"] == "dividend" and a["amount_per_share"] == Decimal("94.00")
    assert parse_subject("Interest Payment") == []


def test_share_multiplier():
    assert share_multiplier("split", Decimal(10), Decimal(2)) == 5
    assert share_multiplier("bonus", Decimal(1), Decimal(1)) == 2
    assert share_multiplier("dividend", None, None) == 1


def test_parse_constituents():
    rows = parse_constituents("Company Name,Industry,Symbol,Series,ISIN Code\nABB India Ltd.,Capital Goods,ABB,EQ,INE117A01022\n")
    assert rows == [{"symbol": "ABB", "name": "ABB India Ltd.", "industry": "Capital Goods", "series": "EQ",
                     "isin": "INE117A01022"}]


# -- EOD pipeline ------------------------------------------------------------------
def chart(close: float, volume: int, day: date) -> dict:
    ts = int(datetime.combine(day, time(9, 15), IST).timestamp())
    return {"chart": {"result": [{"timestamp": [ts], "indicators": {"quote": [
        {"open": [close], "high": [close], "low": [close], "close": [close], "volume": [volume]}]}}]}}


def fake_yahoo(closes: dict[str, float], volumes: dict[str, int]) -> YahooFinance:
    def fetch(ticker, p1, p2):
        sym = ticker[:-3]
        if sym not in closes:
            return {"chart": {"result": []}}
        return chart(closes[sym], volumes[sym], DAY)
    return YahooFinance(fetch_chart=fetch, bucket=FAST)


class Env:
    def __init__(self, tmp_path, *, yahoo_closes=None, nse_fails=False, actions=()):
        self.engine = make_engine("sqlite:///:memory:")
        m.Base.metadata.create_all(self.engine)
        self.f = session_factory(self.engine)
        self.settings = Settings(universe="NIFTY200", reconcile_sample_size=3)
        cache = DiskCache(tmp_path / "cache")
        eq = parse_equity_bhavcopy(udiff())
        ours = {r["symbol"]: float(r["close"]) for r in eq}
        volumes = {r["symbol"]: r["volume"] for r in eq}
        self.ours = ours
        closes = yahoo_closes or {s: ours[s] for s in SYMBOLS}

        def equity(d):
            if nse_fails:
                raise ConnectionError("blocked")
            return udiff()

        self.nse = NseArchives(cache, bucket=FAST, fetchers={
            "equity": equity, "full": lambda d: (FX / "full_2026-10-12.csv").read_text(),
            "index": lambda d: (FX / "index_2026-10-12.csv").read_text()})
        self.yahoo = fake_yahoo(closes, volumes)
        self.corp = NseCorporateActions(cache, getter=lambda params: list(actions), bucket=FAST)
        with self.f() as s, s.begin():
            seed(s)
            sync_calendar(s, date(2026, 10, 1), date(2026, 10, 31), [{"date": date(2026, 10, 2), "note": "Gandhi Jayanti"}])
            sync_universe(s, "NIFTY200", [{"symbol": x, "name": x, "industry": "Test", "series": "EQ", "isin": ""}
                                          for x in SYMBOLS], date(2026, 10, 1))

    def ingest(self, day=DAY, **kw):
        import ingestion.adapters.base as base
        base_sleep = base.time.sleep
        base.time.sleep = lambda *_: None   # no real backoff in tests
        try:
            return run_eod_ingest(self.f, day, nse=self.nse, yahoo=self.yahoo, corp=self.corp,
                                  settings=self.settings, **kw)
        finally:
            base.time.sleep = base_sleep

    def gate(self, day=DAY):
        with self.f() as s, s.begin():
            return run_gate(s, day, self.settings)

    def count(self, model, *where):
        with self.f() as s:
            return s.scalar(select(func.count()).select_from(model).where(*where))


def test_eod_ingest_happy_path(tmp_path):
    env = Env(tmp_path)
    rep = env.ingest()
    assert rep.equity_source == "nse_bhavcopy" and rep.equity_rows == 7 and rep.index_rows == 8
    assert rep.anomalies == []
    with env.f() as s:
        p = s.scalar(select(m.PriceDaily).join(m.Instrument).where(m.Instrument.symbol == "INFY"))
        assert p.delivery_pct is not None and p.adj_close == p.close
    g = env.gate()
    assert g.passed and g.excluded == [] and g.stats["fresh_fraction"] == 1.0
    assert env.count(m.DataGateResult) == 1


def test_ingest_is_idempotent(tmp_path):
    env = Env(tmp_path)
    env.ingest()
    env.ingest()
    assert env.count(m.PriceDaily) == 7


def test_holiday_is_skipped(tmp_path):
    env = Env(tmp_path)
    assert env.ingest(date(2026, 10, 2)).trading_day is False
    g = env.gate(date(2026, 10, 2))
    assert not g.is_trading_day


def test_reconciliation_mismatch_excludes_stock(tmp_path):
    env = Env(tmp_path)
    closes = {s: env.ours[s] for s in SYMBOLS}
    closes["RELIANCE"] *= 1.05
    env = Env(tmp_path, yahoo_closes=closes)
    rep = env.ingest(reconcile_symbols=["RELIANCE"])
    assert [a["symbol"] for a in rep.anomalies] == ["RELIANCE"]
    assert env.count(m.HitlTask, m.HitlTask.task_type == "data_anomaly") == 1
    g = env.gate()
    assert "RELIANCE" in g.excluded


def test_primary_failure_falls_back_to_yahoo(tmp_path):
    env = Env(tmp_path, nse_fails=True)
    rep = env.ingest()
    assert rep.equity_source == "yfinance" and rep.equity_rows == len(SYMBOLS)
    g = env.gate()
    assert g.passed and any("primary source unhealthy" in i for i in g.issues)


def test_everything_down_fails_gate(tmp_path):
    env = Env(tmp_path, nse_fails=True)
    env.yahoo = YahooFinance(fetch_chart=lambda *a: (_ for _ in ()).throw(ConnectionError("down")), bucket=FAST)
    env.ingest()
    g = env.gate()
    assert not g.passed and any("freshness" in i for i in g.issues)


def test_split_adjusts_history_and_gate_accepts_it(tmp_path):
    split = {"symbol": "ITC", "series": "EQ", "isin": "", "exDate": DAY.strftime("%d-%b-%Y"),
             "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"}
    env = Env(tmp_path, actions=[split])
    itc_close = Decimal(str(env.ours["ITC"]))
    with env.f() as s, s.begin():   # yesterday, pre-split price 5x higher
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == "ITC"))
        s.add(m.PriceDaily(instrument_id=iid, trade_date=PREV, open=itc_close * 5, high=itc_close * 5,
                           low=itc_close * 5, close=itc_close * 5, source="nse_bhavcopy"))
    env.ingest()
    with env.f() as s:
        prev = s.get(m.PriceDaily, (iid, PREV))
        assert prev.adj_close == (itc_close * 5 / 5).quantize(Decimal("0.0001"))
    assert "ITC" not in env.gate().excluded


def test_unexplained_move_excluded_once(tmp_path):
    env = Env(tmp_path)
    env.ingest()
    with env.f() as s, s.begin():
        p = s.scalar(select(m.PriceDaily).join(m.Instrument).where(m.Instrument.symbol == "SBIN"))
        p.prev_close = p.close / 2
    assert "SBIN" in env.gate().excluded
    env.gate()
    assert env.count(m.HitlTask, m.HitlTask.task_type == "data_anomaly") == 1


def test_universe_membership_is_point_in_time(tmp_path):
    env = Env(tmp_path)
    with env.f() as s, s.begin():
        sync_universe(s, "NIFTY200", [{"symbol": x, "name": x, "industry": "Test", "series": "EQ", "isin": ""}
                                      for x in SYMBOLS if x != "ITC"], date(2026, 10, 10))
    with env.f() as s:
        assert "ITC" in universe_members(s, "NIFTY200", date(2026, 10, 9)).values()
        assert "ITC" not in universe_members(s, "NIFTY200", date(2026, 10, 12)).values()


def test_raw_files_are_cached(tmp_path):
    env = Env(tmp_path)
    calls = []
    env.nse._fetch["equity"] = lambda d: calls.append(d) or udiff()
    env.nse.equity(DAY)
    env.nse.equity(DAY)
    assert len(calls) == 1


def test_empty_reconciliation_is_reported_not_silent(tmp_path):
    env = Env(tmp_path)
    env.yahoo = YahooFinance(fetch_chart=lambda *a: {"chart": {"result": []}}, bucket=FAST)
    rep = env.ingest()
    assert rep.reconciled is False
    assert any("reconciliation skipped" in w for w in rep.warnings)
    with env.f() as s:
        h = s.scalar(select(m.SourceHealth).where(m.SourceHealth.source == "yfinance"))
        assert h.ok is False
