"""Phase 3: context ingestion, privacy filtering, news clusters, event overlay, regime inputs,
transmission-map review, pre-market brief, and the market API."""
import asyncio
import time as _time
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from core import models as m
from core.settings import Settings
from core.store import SqlStore
from hitl.service import HitlService
from ingestion.adapters.base import FetchResult, TokenBucket, utcnow
from ingestion.adapters.macro import parse_series
from ingestion.adapters.nse_extra import parse_board_meetings, parse_flows
from ingestion.adapters.world_news import WorldNews, clean, parse_gdelt, parse_rss
from ingestion.context import jaccard, run_context_ingest, store_news, title_tokens
from orchestrator.budget import BudgetManager
from orchestrator.config import Config
from orchestrator.deps import Deps
from orchestrator.fakes import RecordingNotifier, ScriptedLLM
from orchestrator.map_review import run_map_review
from orchestrator.ports import AllowlistedTools
from orchestrator.premarket import gather_brief, render_brief
from quant.engine import QuantEngine, event_overlay

from .synth import AS_OF, DECISION, IST, synthetic_db
from .test_tools import make_service

UTC = timezone.utc


# -- parsers & privacy -------------------------------------------------------------------
def test_parse_macro_series_dedupes_days():
    ts = int(datetime(2026, 10, 12, 20, tzinfo=UTC).timestamp())
    payload = {"chart": {"result": [{"meta": {"gmtoffset": -14400}, "timestamp": [ts, ts + 60],
                                     "indicators": {"quote": [{"close": [105.8, 105.9]}]}}]}}
    (r,) = parse_series("BRENT", payload)
    assert r["obs_date"] == date(2026, 10, 12) and r["value"] == Decimal("105.8")


def test_parse_flows_and_board_meetings():
    rows = parse_flows([{"category": "DII", "date": "13-Oct-2026", "netValue": "2439.24"},
                        {"category": "FII/FPI", "date": "13-Oct-2026", "netValue": "-1,305.13"}])
    assert rows == [{"trade_date": date(2026, 10, 13), "fii_net": Decimal("-1305.13"), "dii_net": Decimal("2439.24")}]
    bm = parse_board_meetings([{"bm_symbol": "TCS", "bm_date": "17-Oct-2026", "bm_purpose": "Financial Results", "bm_desc": "Q2"},
                               {"bm_symbol": "X", "bm_date": "18-Oct-2026", "bm_purpose": "Fund Raising", "bm_desc": ""}])
    assert [b["is_results"] for b in bm] == [True, False]


def test_rss_bom_bytes_dates_and_privacy():
    xml = ("﻿<?xml version='1.0' encoding='utf-8'?><rss><channel>"
           "<item><title>RBI keeps repo rate unchanged</title><link>https://rbi/1</link><pubDate>13 Oct, 2026 +0530</pubDate>"
           "<description>Policy statement ABCDE1234F</description></item>"
           "<item><title>Notice of attachment issued against Some Person</title><link>https://sebi/2</link><pubDate>13 Oct, 2026 +0530</pubDate></item>"
           "<item><title>Appeal No. 7109 of 2026 filed by Someone</title><link>https://sebi/3</link><pubDate>13 Oct, 2026 +0530</pubDate></item>"
           "<item><title>भारत की</title><link>https://pib/4</link><pubDate>13 Oct, 2026 +0530</pubDate></item>"
           "</channel></rss>").encode("utf-8")
    items = parse_rss(xml, "RBI")
    assert [i["title"] for i in items] == ["RBI keeps repo rate unchanged"]
    assert "ABCDE1234F" not in items[0]["snippet"] and "[redacted]" in items[0]["snippet"]
    assert items[0]["published_at"] == datetime(2026, 10, 12, 18, 30, tzinfo=UTC)
    assert clean({"title": "Order against XYZAB1234C", "snippet": ""}) is None


def test_gdelt_falls_back_to_google_news():
    from ingestion.adapters.gnews import GoogleNews

    from .synth import rss

    def gdelt_429(url, params):
        raise RuntimeError("429")
    g = GoogleNews(getter=lambda url: rss([("Brent jumps on supply fears", "Reuters", datetime.now(UTC))]),
                   bucket=TokenBucket(1000, 1000))
    w = WorldNews(get_json=gdelt_429, gnews=g, gdelt_bucket=TokenBucket(1000, 1000))
    import ingestion.adapters.base as base
    saved, base.time.sleep = base.time.sleep, lambda *_: None
    try:
        res = w.themes()
    finally:
        base.time.sleep = saved
    assert len(res.records) == 4 and all(r["title"] == "Brent jumps on supply fears" for r in res.records)
    assert any("gdelt" in x for x in res.warnings)
    assert parse_gdelt({"articles": [{"url": "u", "title": "t", "seendate": "20261013T010000Z", "domain": "d"}]})[0]["source"] == "d"


def test_news_clusters_near_duplicates():
    assert jaccard(title_tokens("Brent crude jumps 3% on supply concerns"),
                   title_tokens("Brent crude jumps 3% on supply concerns - analysts")) >= 0.6
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=30)
    now = datetime.now(UTC)
    items = [{"url": f"https://n/{i}", "url_hash": str(i), "title": t, "source": "s", "published_at": now, "snippet": ""}
             for i, t in enumerate(["Brent crude jumps 3% on supply concerns", "Brent crude jumps 3% on supply concerns today",
                                    "RBI holds repo rate"])]
    with f() as s, s.begin():
        assert store_news(s, items) == 3
        assert store_news(s, items) == 0
    with f() as s:
        clusters = [n.dedupe_cluster_id for n in s.scalars(select(m.NewsItem).order_by(m.NewsItem.id))]
    assert clusters[0] == clusters[1] != clusters[2]


# -- context ingest ----------------------------------------------------------------------
class FakeMacro:
    def fetch(self, start, end, series=None):
        rows = []
        for i in range(8):
            d = end - timedelta(days=7 - i)
            rows += [{"series_id": "USDINR", "obs_date": d, "value": Decimal(str(84 + i * 0.2))},
                     {"series_id": "BRENT", "obs_date": d, "value": Decimal(str(80 + i))},
                     {"series_id": "US10Y", "obs_date": d, "value": Decimal(str(4.0 + i * 0.05))}]
        return FetchResult(rows, "yahoo_macro", utcnow())


class FakeExtra:
    def flows(self):
        return FetchResult([{"trade_date": AS_OF - timedelta(days=i), "fii_net": Decimal("-2500"), "dii_net": Decimal("3000")}
                            for i in range(5)], "nse_fiidii", utcnow())

    def board_meetings(self, start=None, end=None):
        return FetchResult([{"symbol": "UP0", "date": AS_OF + timedelta(days=3), "purpose": "Financial Results",
                             "is_results": True, "desc": "Q2"}], "nse_board_meetings", utcnow())


class FakeWorld:
    def themes(self):
        return FetchResult([{"url": "https://w/1", "url_hash": "1", "title": "Oil rallies on OPEC cut", "source": "x",
                             "published_at": datetime.now(UTC), "snippet": ""}], "world_news", utcnow())

    def official(self):
        raise ConnectionError("rss down")


def test_context_ingest_isolates_failures():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=30)
    rep = run_context_ingest(f, AS_OF, macro=FakeMacro(), nse_extra=FakeExtra(), world=FakeWorld())
    assert rep.macro_rows == 24 and rep.flows_rows == 5 and rep.results_dates == 1 and rep.world_news_new == 1
    assert any(w.startswith("official_rss") for w in rep.warnings)
    with f() as s:
        assert s.scalar(select(func.count()).select_from(m.CorporateAction).where(m.CorporateAction.action_type == "results")) == 1
        health = {h.source: h.ok for h in s.scalars(select(m.SourceHealth))}
    assert health["official_rss"] is False and health["yahoo_macro"] is True


# -- event overlay & regime ----------------------------------------------------------------
def add_event(f, *, sector, direction, magnitude, days_ago=0, decay=10, confidence=0.8, severity=3):
    with f() as s, s.begin():
        first = datetime.combine(AS_OF - timedelta(days=days_ago), time(8), IST)
        e = m.Event(event_key=f"k{sector}{direction}{days_ago}{magnitude}", event_type="commodity", title="t",
                    severity=severity, status="active", first_seen=first, source_news_ids=[1, 2])
        s.add(e)
        s.flush()
        s.add(m.EventImpact(event_id=e.id, sector_id=SqlStore.sector_id(s, sector), direction=direction,
                            magnitude=Decimal(str(magnitude)), horizon="short",
                            details={"decay_days": decay, "confidence": confidence}))
        return e.id


def test_event_overlay_decays_and_signs():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=30)
    add_event(f, sector="Financial Services", direction=-1, magnitude=0.5, days_ago=5, decay=10)
    add_event(f, sector="Financial Services", direction=-1, magnitude=0.5, days_ago=12, decay=10)   # fully decayed
    add_event(f, sector="Information Technology", direction=1, magnitude=0.4)
    with f() as s:
        sectors, _ = event_overlay(s, AS_OF)
    assert sectors["Financial Services"] == pytest.approx(-0.5 * 0.8 * 0.5)
    assert sectors["Information Technology"] == pytest.approx(0.4 * 0.8)


def test_event_overlay_moves_quant_scores():
    _, f = synthetic_db()
    qe = QuantEngine(f, Settings(universe="NIFTY200"))
    before = qe.score_horizon("FLAT0", "short", AS_OF)["raw_score"]
    add_event(f, sector="Financial Services", direction=1, magnitude=0.8)       # FLAT* and DN* are Financial Services
    qe2 = QuantEngine(f, Settings(universe="NIFTY200"))
    after = qe2.score_horizon("FLAT0", "short", AS_OF)
    assert after["raw_score"] > before and "event" not in after["data_gaps"]
    assert qe2.snapshot(AS_OF).sector_events["Financial Services"] > 0


def test_regime_uses_flows_and_macro():
    _, f = synthetic_db()
    run_context_ingest(f, AS_OF, macro=FakeMacro(), nse_extra=FakeExtra())
    r = QuantEngine(f, Settings(universe="NIFTY200")).regime_inputs(AS_OF)
    assert r["fii_5d_cr"] == -12500 and r["usdinr_chg_5d_pct"] > 0 and r["brent_chg_5d_pct"] > 0
    assert r["unavailable"] == []


# -- transmission map review ----------------------------------------------------------------
def deps_for(f):
    store = SqlStore(f)
    return Deps(Config(), AllowlistedTools(make_service(f)), store, ScriptedLLM(record_to=store), RecordingNotifier(),
                BudgetManager(5, 5))


def test_map_review_validates_and_review_applies():
    _, f = synthetic_db(n_up=2, n_flat=0, n_down=2, days=80)
    for i in range(3):
        add_event(f, sector="Automobile and Auto Components", direction=-1, magnitude=0.5, days_ago=i)
    deps = deps_for(f)
    out = asyncio.run(run_map_review(deps, DECISION))
    assert len(out["proposals"]) == 1
    p = out["proposals"][0]
    assert p["magnitude"] == 0.8 and p["action"] in ("add", "modify")            # capped at 0.8
    assert any("unknown factor or sector" in d for d in out["dropped"])
    assert any("supporting event(s) <" in d for d in out["dropped"])
    (tid,) = out["tasks"]
    with f() as s:
        row = s.get(m.SectorSensitivity, s.get(m.HitlTask, tid).subject_id)
        assert row.status == "proposed" and row.proposed_by == "LLM-13"
    old_id = p["current_row_id"]
    HitlService(f).decide(tid, "approve")
    with f() as s:
        row = s.get(m.SectorSensitivity, s.get(m.HitlTask, tid).subject_id)
        assert row.status == "approved"
        if old_id:
            assert s.get(m.SectorSensitivity, old_id).status == "retired"


def test_map_review_skips_without_enough_events():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=30)
    out = asyncio.run(run_map_review(deps_for(f), DECISION))
    assert out["proposals"] == [] and "only 0 events" in out["skipped"]


def test_seed_map_is_present_and_marked_for_review():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=30)
    with f() as s:
        rows = s.scalars(select(m.SectorSensitivity)).all()
    assert len(rows) >= 15 and all(r.status == "approved" and r.approved_by == "seed: review me" for r in rows)


# -- pre-market brief ------------------------------------------------------------------------
def test_premarket_brief():
    _, f = synthetic_db(n_up=2, n_flat=0, n_down=0, days=60)
    run_context_ingest(f, AS_OF, macro=FakeMacro(), nse_extra=FakeExtra())
    add_event(f, sector="Information Technology", direction=1, magnitude=0.4, severity=4)
    with f() as s, s.begin():
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == "UP0"))
        last = s.scalar(select(m.PriceDaily.close).where(m.PriceDaily.instrument_id == iid).order_by(m.PriceDaily.trade_date.desc()).limit(1))
        run = m.Run(run_type="daily", decision_time=DECISION, status="succeeded")
        s.add(run)
        s.flush()
        s.add(m.Recommendation(run_id=run.id, instrument_id=iid, horizon="short", verdict="favourable", quant_score=80,
                               final_score=80, entry_low=last, entry_high=last, stop=last * Decimal("0.99"),
                               target=last * 2, card={}, status="active", published_at=DECISION))
    b = gather_brief(f, AS_OF + timedelta(days=1))
    text = render_brief(b)
    assert b["flows"]["fii_net_cr"] == -2500 and len(b["macro"]) == 3
    assert any("within 2% of its stop" in a for a in b["attention"]) and any("results on" in a for a in b["attention"])
    assert "Pre-market brief" in text and "FII -2,500 cr" in text and "Decision-support only" in text


# -- API ---------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    _, f = synthetic_db()
    run_context_ingest(f, AS_OF, macro=FakeMacro(), nse_extra=FakeExtra())
    add_event(f, sector="Information Technology", direction=1, magnitude=0.5)

    @asynccontextmanager
    async def deps_cm():
        yield deps_for(f)

    from api.main import create_app

    with TestClient(create_app(deps_cm, f)) as c:
        yield c


def test_market_endpoints(client):
    mac = client.get("/api/v1/market/macro").json()
    assert mac["series"]["BRENT"]["last"] == 87.0 and mac["series"]["US10Y"]["unit"] == "pp" and len(mac["flows"]) == 5
    sec = {r["sector"]: r for r in client.get("/api/v1/market/sectors").json()["sectors"]}
    assert sec["Information Technology"]["event_overlay"] > 0 and sec["Information Technology"]["ret_1m_pct"] is not None
    cal = client.get("/api/v1/market/calendar", params={"days": 30, "start": AS_OF.isoformat()}).json()["items"]
    assert any(i["kind"] == "results" and i["symbol"] == "UP0" for i in cal)
    assert any(i["kind"] == "holiday" and i["detail"] == "Diwali" for i in cal)
    sens = client.get("/api/v1/market/sensitivity").json()["rows"]
    assert sens and all(r["needs_review"] for r in sens)


def test_discover_job(client):
    r = client.post("/api/v1/discover", json={"horizons": ["short"], "max_ideas": 2})
    assert r.status_code == 202
    jid = r.json()["job_id"]
    for _ in range(300):
        j = client.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] != "running":
            break
        _time.sleep(0.2)
    assert j["status"] == "succeeded", j["error"]
    assert j["kind"] == "discover" and len(j["result"]["results"]) == 2
    assert j["events"][-1]["message"] == "Discovery complete"
    assert client.post("/api/v1/discover", json={"horizons": ["weekly"]}).status_code == 422
