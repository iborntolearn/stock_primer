"""Phase 4: XBRL fundamentals and their checks, LLM-15 text fallback, scorecard attribution,
event-triggered reruns, results follow-up, grounded chat, autonomy approval and the LLM cache."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from core import models as m
from core.settings import Settings
from core.store import SqlStore
from evaluation.scorecard import _agent_attribution, summary
from hitl.service import HitlService
from ingestion.adapters.nse_results import ResultFiling, parse_listing, parse_xbrl
from ingestion.filing_text import extract_from_text, to_rupees
from ingestion.fundamentals import check_statement, ingest_symbol
from orchestrator.budget import BudgetManager
from orchestrator.config import Config
from orchestrator.deps import Deps
from orchestrator.fakes import RecordingNotifier, ScriptedLLM
from orchestrator.llm import ClaudeLLM
from orchestrator.ports import AllowlistedTools
from orchestrator.schemas import RouteDecision
from orchestrator.wiring import load_config
from scheduler import jobs

from .synth import AS_OF, DECISION, IST, synthetic_db
from .test_api import wait_job
from .test_tools import call, make_service
from .test_units import client as stub_client, resp, GOOD_ROUTE

CR = Decimal(10_000_000)


def xbrl(period_end: str, *, revenue=1000, other=50, pbt=200, tax=50, pat=150, eps="15", cap=100, fv=10,
         bank=False, nature="Consolidated") -> str:
    """A minimal XBRL instance in rupees (inputs in crore)."""
    def tag(name, value):
        return f'<in-capmkt:{name} contextRef="OneD" unitRef="INR" decimals="-5">{value}</in-capmkt:{name}>'
    rev_tag = "InterestEarned" if bank else "RevenueFromOperations"
    parts = [tag(rev_tag, revenue * CR), tag("OtherIncome", other * CR), tag("Income", (revenue + other) * CR),
             tag("ProfitBeforeTax", pbt * CR), tag("TaxExpense", tax * CR), tag("ProfitLossForPeriod", pat * CR),
             tag("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", eps),
             tag("PaidUpValueOfEquityShareCapital", cap * CR), tag("FaceValueOfEquityShareCapital", fv),
             tag("DateOfEndOfReportingPeriod", period_end), tag("NatureOfReportStandaloneConsolidated", nature)]
    if bank:
        parts.append(tag("OperatingProfitBeforeProvisionAndContingencies", 300 * CR))
    return "<xbrli:xbrl>" + "".join(parts) + "</xbrli:xbrl>"


# -- XBRL parsing & deterministic checks ----------------------------------------------------
def test_parse_xbrl_company_and_bank():
    q = parse_xbrl(xbrl("2026-06-30"))
    assert q["revenue"] == 1000 * CR and q["pat"] == 150 * CR and q["eps_basic"] == Decimal("15")
    assert q["period_end"] == date(2026, 6, 30) and q["is_bank"] is False and q["nature"] == "Consolidated"
    b = parse_xbrl(xbrl("2026-06-30", bank=True))
    assert b["revenue"] == 1000 * CR and b["is_bank"] is True and b["bank_operating_profit"] == 300 * CR


def test_check_statement_flags_inconsistencies():
    good = parse_xbrl(xbrl("2026-06-30"))
    assert check_statement(good) == []
    assert any("PBT - tax" in i for i in check_statement(parse_xbrl(xbrl("2026-06-30", pat=60, eps="6"))))
    assert any("EPS" in i for i in check_statement(parse_xbrl(xbrl("2026-06-30", eps="40"))))
    assert check_statement({"revenue": None, "pat": 1}) == ["revenue or profit missing"]
    prior = parse_xbrl(xbrl("2025-06-30", revenue=100, other=5, pbt=20, tax=5, pat=15, eps="1.5"))
    assert any("YoY" in i for i in check_statement(good, prior))


def test_parse_listing_skips_null_xbrl_and_non_quarterly():
    rows = [{"symbol": "ABC", "toDate": "30-Jun-2026", "broadCastDate": "20-Jul-2026 18:05:00", "consolidated": "Consolidated",
             "period": "Quarterly", "xbrl": "https://x/1.xml"},
            {"symbol": "ABC", "toDate": "31-Mar-2026", "broadCastDate": "20-Apr-2026", "consolidated": "Non-Consolidated",
             "period": "Annual", "xbrl": "https://x/2.xml"},
            {"symbol": "ABC", "toDate": "31-Mar-2026", "broadCastDate": "20-Apr-2026", "xbrl": "https://x/null"}]
    (f,) = parse_listing(rows, "nse_legacy")
    assert f.consolidated and f.period_end == date(2026, 6, 30) and f.published_at.hour == 18


class FakeResults:
    def __init__(self, docs):
        self.docs = docs          # url -> (ResultFiling, xml)
        self.fetched = []

    def filings(self, symbol):
        return [fl for fl, _ in self.docs.values() if fl.symbol == symbol]

    def xbrl(self, fl):
        self.fetched.append(fl.xbrl_url)
        return self.docs[fl.xbrl_url][1]


def filing(sym, pe, cons, url, pub=None):
    return ResultFiling(sym, pe, cons, pub or datetime.combine(pe + timedelta(days=25), time(17)), url, "nse_legacy")


def test_ingest_prefers_consolidated_and_routes_bad_quarters_to_review():
    _, f = synthetic_db(n_up=2, n_flat=0, n_down=0, days=40)
    pes = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]
    docs = {}
    for i, pe in enumerate(pes):
        docs[f"c{i}"] = (filing("UP0", pe, True, f"c{i}"), xbrl(pe.isoformat(), revenue=1000 + 10 * i))
        docs[f"s{i}"] = (filing("UP0", pe, False, f"s{i}"), xbrl(pe.isoformat(), revenue=500, nature="Standalone"))
    docs["c3"] = (docs["c3"][0], xbrl("2026-06-30", pat=60, eps="6"))      # PAT inconsistent with PBT - tax
    src = FakeResults(docs)
    rep = ingest_symbol(f, src, "UP0", quarters=4)
    assert rep.stored == 3 and len(rep.anomalies) == 1 and not rep.errors
    assert all(u.startswith("c") for u in src.fetched)
    with f() as s:
        snaps = s.scalars(select(m.FundamentalSnapshot).order_by(m.FundamentalSnapshot.period_end)).all()
        assert [x.source for x in snaps] == ["nse_xbrl_consolidated"] * 3
        assert snaps[0].revenue == Decimal("1000.00") and snaps[0].pat == Decimal("150.00")
        assert snaps[0].published_at == pes[0] + timedelta(days=25)
        task = s.scalar(select(m.HitlTask).where(m.HitlTask.task_type == "data_anomaly"))
        assert task.payload["period_end"] == "2026-06-30"
    again = ingest_symbol(f, src, "UP0", quarters=4)
    assert again.skipped_existing == 3 and again.stored == 0


def test_fundamental_tools_are_point_in_time():
    _, f = synthetic_db(n_up=2, n_flat=0, n_down=0, days=40)
    pes = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]
    ingest_symbol(f, FakeResults({f"c{i}": (filing("UP0", pe, True, f"c{i}"), xbrl(pe.isoformat())) for i, pe in enumerate(pes)}),
                  "UP0", quarters=4)
    svc = make_service(f)
    fin = call(svc, "fundamental", "get_financials", {"symbol": "UP0"})
    assert fin.ok and len(fin.data["periods"]) == 4
    early = call(svc, "fundamental", "get_financials", {"symbol": "UP0"}, as_of=datetime(2026, 1, 1, tzinfo=IST))
    assert len(early.data["periods"]) == 1                 # only Sep-25 was published by 1 Jan 2026
    ratios = call(svc, "fundamental", "get_ratios", {"symbol": "UP0"})
    assert ratios.ok and ratios.data["ttm_eps"] == 60.0
    assert call(svc, "fundamental", "get_shareholding", {"symbol": "UP0"}).ok is False


# -- LLM-15 text fallback -----------------------------------------------------------------
def deps_for(f, llm=None):
    store = SqlStore(f)
    return Deps(Config(), AllowlistedTools(make_service(f)), store, llm or ScriptedLLM(record_to=store), RecordingNotifier(),
                BudgetManager(5, 5))


def test_text_extraction_stores_or_routes_to_review():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    deps = deps_for(f)
    out = asyncio.run(extract_from_text(deps, f, "UP0", date(2026, 6, 30), "Revenue 1000 ...", date(2026, 7, 25), DECISION))
    assert out["status"] == "stored" and not out["issues"]
    with f() as s:
        snap = s.scalar(select(m.FundamentalSnapshot))
        assert snap.source == "llm15_consolidated" and snap.revenue == Decimal("1000.00") and snap.pat == Decimal("150.00")
    unit = SimpleNamespace(value=None, page=None)
    lines = SimpleNamespace(**{k: SimpleNamespace(value=1.0, page=1) for k in (
        "revenue", "other_income", "total_income", "profit_before_tax", "tax_expense", "net_profit",
        "net_profit_owners", "eps_basic", "paid_up_capital", "face_value")})
    assert to_rupees(lines, "INR_lakh")["revenue"] == Decimal(100000)
    assert to_rupees(lines, "furlongs") is None and unit.value is None


# -- scorecard attribution --------------------------------------------------------------------
def test_agent_attribution_splits_by_direction():
    def item(points, alpha):
        r = SimpleNamespace(meta={"contributions": [{"source": "technical", "points": points},
                                                    {"source": "critic", "points": 0}]})
        o = SimpleNamespace(alpha_pct=alpha, hit_target=alpha > 0, hit_stop=alpha < 0, ret_pct=alpha, days_held=5)
        return r, o
    items = [item(3, 2.0), item(2, 1.0), item(-4, -3.0)]
    (tech,) = _agent_attribution(items)
    assert tech["source"] == "technical"
    assert tech["raised"]["n"] == 2 and tech["raised"]["avg_alpha_pct"] == 1.5
    assert tech["lowered"]["n"] == 1 and tech["lowered"]["positive_alpha_rate"] == 0.0
    assert summary([])["n"] == 0 and summary(items)["sufficient_sample"] is False


# -- event reruns & results follow-up -----------------------------------------------------------
def runtime(f, now):
    return jobs.Runtime(deps=deps_for(f), f=f, settings=Settings(universe="NIFTY200"), adapters=lambda: None,
                        now=lambda: now)


def test_pending_event_runs_execute_once():
    from .test_phase3 import add_event
    _, f = synthetic_db(n_up=2, n_flat=0, n_down=0, days=60)
    eid = add_event(f, sector="Information Technology", direction=-1, magnitude=0.6, severity=5)
    rt = runtime(f, DECISION)
    tid = rt.deps.store.create_hitl_task("high_severity_event", "event", eid, payload={}, ai_recommendation="confirm",
                                         reason="test", priority=1, default_on_expiry="reject")
    HitlService(f).decide(tid, "trigger_run", now=DECISION)
    out = asyncio.run(jobs.pending_event_runs_job(rt))
    assert len(out) == 1 and out[0]["events"] == [eid]
    with f() as s:
        assert s.get(m.HitlTask, tid).decision["run_done"] is True
        assert s.scalar(select(func.count()).select_from(m.Run).where(m.Run.run_type == "event_triggered")) == 1
    assert asyncio.run(jobs.pending_event_runs_job(rt)) == []


def test_results_followup_ingests_symbols_with_board_meetings():
    _, f = synthetic_db(n_up=2, n_flat=0, n_down=0, days=40)
    with f() as s, s.begin():
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.symbol == "UP1"))
        s.add(m.CorporateAction(instrument_id=iid, action_type="results", ex_date=AS_OF - timedelta(days=1), details={}, source="nse_board"))
    rt = runtime(f, DECISION)
    pe = date(2026, 9, 30)
    rt.results_source = FakeResults({"u": (filing("UP1", pe, True, "u", pub=datetime(2026, 10, 11, 17)), xbrl(pe.isoformat()))})
    out = asyncio.run(jobs.results_followup_job(rt, AS_OF))
    assert out["symbols"] == ["UP1"] and out["stored"] == 1


# -- API: chat & autonomy ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def api():
    _, f = synthetic_db()

    @asynccontextmanager
    async def deps_cm():
        yield deps_for(f)

    from api.main import create_app

    with TestClient(create_app(deps_cm, f)) as c:
        c.f = f
        yield c


def test_chat_job_answers_with_citations(api):
    r = api.post("/api/v1/chat", json={"message": "How has UP2 moved this month?", "history": []})
    assert r.status_code == 202
    job = wait_job(api, r.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["reply"] and job["result"]["intent"]


def test_autonomy_change_needs_approval(api):
    assert load_config(api.f).autonomy["publish_short"] == 1
    r = api.post("/api/v1/config/autonomy", json={"action": "publish_short", "level": 2, "note": "scorecard ok"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert load_config(api.f).autonomy["publish_short"] == 1          # nothing changes until approved
    HitlService(api.f).decide(body["task_id"], "approve", now=datetime.now(IST))
    cfg = load_config(api.f)
    assert cfg.autonomy["publish_short"] == 2 and cfg.version == body["config_version"]
    assert api.post("/api/v1/config/autonomy", json={"action": "publish_short", "level": 2}).status_code == 409


# -- LLM response cache ------------------------------------------------------------------------
def test_identical_calls_reuse_cached_response():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=10)
    store = SqlStore(f)
    run_id = store.create_run("on_demand", DECISION, "t", 1, "s", "p")
    c, stub = stub_client([resp(GOOD_ROUTE)])
    llm = ClaudeLLM(Config(), store, BudgetManager(5, 5), client=c)

    def ask():
        return asyncio.run(llm.structured(call_id="LLM-01", agent="router", run_id=run_id, decision_time=DECISION,
                                          sections={"message": "INFY?", "session_context": ""}, output_model=RouteDecision))
    assert ask().symbols == ["INFY"] and ask().symbols == ["INFY"]
    assert len(stub.requests) == 1
    with f() as s:
        assert sorted(s.scalars(select(m.LlmCall.validation_status))) == ["cached", "passed"]


def test_consolidated_profit_includes_associates_and_discontinued():
    from ingestion.fundamentals import reconcile_owners
    q = parse_xbrl(xbrl("2025-06-30", pbt=200, tax=50, pat=180, eps="18"))
    assert any("PBT - tax" in i for i in check_statement(q))
    assert check_statement({**q, "associates": 30 * CR}) == []                 # 150 + 30 associates = 180
    assert check_statement({**q, "discontinued": 30 * CR}) == []
    # A misfiled owners' figure (far below total, minority interest doesn't explain it) falls back to total profit.
    bad = reconcile_owners({**q, "pat_owners": 1 * CR, "nci": 0})
    assert bad["pat_owners"] is None and "owners_note" in bad
    good = reconcile_owners({**q, "pat_owners": 170 * CR, "nci": 10 * CR})
    assert good["pat_owners"] == 170 * CR


def test_eps_only_mismatch_stores_without_eps_and_unsupported_formats_skip_review():
    _, f = synthetic_db(n_up=1, n_flat=0, n_down=0, days=20)
    pe1, pe2 = date(2026, 3, 31), date(2026, 6, 30)
    no_rev = xbrl(pe2.isoformat()).replace("RevenueFromOperations", "PremiumsEarned")
    rep = ingest_symbol(f, FakeResults({"a": (filing("UP0", pe1, True, "a"), xbrl(pe1.isoformat(), eps="40")),
                                        "b": (filing("UP0", pe2, True, "b"), no_rev)}), "UP0", quarters=4)
    assert rep.stored == 1 and any("stored without EPS" in a for a in rep.anomalies)
    assert any("unsupported statement format" in e for e in rep.errors)
    with f() as s:
        snap = s.scalar(select(m.FundamentalSnapshot))
        assert snap.eps is None and snap.revenue == Decimal("1000.00") and "EPS 40" in snap.raw["eps_note"]
        assert s.scalar(select(func.count()).select_from(m.HitlTask)) == 0


def test_utility_regulatory_deferral_reconciles():
    q = parse_xbrl(xbrl("2025-06-30", pbt=200, tax=50, pat=230, eps="23"))
    assert check_statement({**q, "regulatory": 50 * CR, "associates": 30 * CR}) == []
