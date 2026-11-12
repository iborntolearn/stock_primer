import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.config import Autonomy, Config
from orchestrator.fakes import ScriptedLLM, fake_deps
from orchestrator.graphs import (
    analyze, chat, discover, run_daily, run_event_digest, run_event_triggered, run_portfolio_review,
)

IST = timezone(timedelta(hours=5, minutes=30))
DT = datetime(2026, 10, 13, 18, 30, tzinfo=IST)


def l2_config(**kw) -> Config:
    return Config(autonomy={f"publish_{h}": Autonomy.L2_APPROVE_EXCEPTIONS for h in ("short", "medium", "long")}, **kw)


def run(coro):
    return asyncio.run(coro)


def test_daily_l1_queues_everything_for_review():
    deps = fake_deps()
    out = run(run_daily(deps, DT, horizons=["short"]))
    assert out["status"] == "succeeded"
    assert out["published"] == []
    favourable = [r for r in out["results"] if r["verdict"] == "favourable"]
    assert len(out["pending"]) == len(favourable) > 0
    assert {t["task_type"] for t in deps.store.hitl.values()} == {"approve_reco"}
    assert all(t["default_on_expiry"] == "reject" for t in deps.store.hitl.values())
    assert "awaiting your review" in deps.notifier.sent[0]["text"]


def test_daily_l2_auto_publishes_only_favourable_and_caps_per_horizon():
    deps = fake_deps(l2_config(max_published_per_horizon=2))
    out = run(run_daily(deps, DT, horizons=["short"]))
    assert [r["symbol"] for r in out["published"]] == ["TATAMOTORS", "HDFCBANK"]
    statuses = {r["symbol"]: deps.store.recos[r["reco_id"]]["status"] for r in out["results"]}
    assert statuses["TATAMOTORS"] == "published"
    assert statuses["ASIANPAINT"] == "validated"  # avoid verdicts are recorded, never published


def test_short_horizon_skips_fundamental_agent():
    llm = ScriptedLLM()
    run(run_daily(fake_deps(llm=llm), DT, horizons=["short"]))
    assert "LLM-08" not in llm.calls
    assert llm.calls.count("LLM-07") == llm.calls.count("LLM-09") == 5


def test_kill_switch_blocks_publishing():
    deps = fake_deps(l2_config(kill_switch=True))
    out = run(run_daily(deps, DT, horizons=["short"]))
    assert out["published"] == []
    assert all(t["reason"] == "kill switch on" for t in deps.store.hitl.values())


def test_data_gate_failure_fails_closed():
    llm = ScriptedLLM()
    deps = fake_deps(llm=llm, data_quality_passed=False)
    out = run(run_daily(deps, DT, horizons=["short"]))
    assert out["status"] == "degraded"
    assert out.get("results") == []
    assert set(llm.calls) == {"LLM-16"}  # only the digest explaining why
    assert "Data quality gate failed" in deps.notifier.sent[0]["text"]


def test_critic_reject_blocks_card():
    llm = ScriptedLLM(hooks={"LLM-10": lambda o, s: {**o, "verdict": "reject"}})
    out = run(run_daily(fake_deps(l2_config(), llm=llm), DT, horizons=["short"]))
    assert out["published"] == []
    assert all(r["status"] == "rejected" for r in out["results"])


def test_critic_needs_human_routes_to_hitl_even_at_l2():
    llm = ScriptedLLM(hooks={"LLM-10": lambda o, s: {**o, "verdict": "needs_human"}})
    deps = fake_deps(l2_config(), llm=llm)
    out = run(run_daily(deps, DT, horizons=["short"]))
    assert out["published"] == []
    assert out["pending"] and all(t["reason"] == "validation exception" for t in deps.store.hitl.values())


def test_synthesizer_cannot_move_levels():
    def nudge(o, s):
        o["levels"]["target"] = o["levels"]["target"] * 1.1
        return o
    out = run(run_daily(fake_deps(l2_config(), llm=ScriptedLLM(hooks={"LLM-11": nudge})), DT, horizons=["short"]))
    assert all(r["status"] == "rejected" for r in out["results"])
    assert any(v["code"] == "V4" for r in out["results"] for v in r["violations"])


def test_analyst_failure_degrades_instead_of_failing():
    llm = ScriptedLLM(fail={"LLM-09"})
    out = run(run_daily(fake_deps(llm=llm), DT, horizons=["short"]))
    assert out["status"] == "succeeded"
    r = next(r for r in out["results"] if r["symbol"] == "TATAMOTORS")
    assert any(g.startswith("news:") for g in r["gaps"])
    assert r["confidence"] < 0.65  # one data gap costs confidence


def test_budget_exhaustion_degrades_candidates():
    from orchestrator.budget import BudgetExceeded

    class Broke(ScriptedLLM):
        async def structured(self, **kw):
            if kw["call_id"] in ("LLM-07", "LLM-08", "LLM-09", "LLM-10", "LLM-11"):
                raise BudgetExceeded("over budget")
            return await super().structured(**kw)
    out = run(run_daily(fake_deps(llm=Broke()), DT, horizons=["short"]))
    assert all(r["status"] == "rejected" for r in out["results"])
    assert out["published"] == []


def test_on_demand_analysis_never_publishes():
    deps = fake_deps(l2_config())
    out = run(analyze(deps, "HDFCBANK", DT))
    assert sorted(r["horizon"] for r in out["results"]) == ["long", "medium", "short"]
    assert not any(r["status"] == "published" for r in deps.store.recos.values())
    assert not deps.store.hitl


def test_on_demand_unknown_symbol_asks_for_clarification():
    out = run(analyze(fake_deps(), "NOSUCHCO", DT))
    assert "couldn't find" in out["clarification"]


def test_discover_respects_max_ideas():
    out = run(discover(fake_deps(), DT, horizon="short", max_ideas=2))
    assert len(out["results"]) == 2


def test_chat_routes_to_analysis_and_qa():
    out = run(chat(fake_deps(), "What do you think of TATAMOTORS?", DT))
    assert out["route"]["intent"] == "analyze_stock"
    assert "TATAMOTORS short" in out["reply"]
    out = run(chat(fake_deps(), "How is the market today?", DT))
    assert out["route"]["intent"] == "market_context"
    assert out["citations"] and f"[{out['citations'][0]}]" in out["reply"]


def test_event_digest_stores_events_caps_inference_and_raises_hitl():
    deps = fake_deps()
    out = run(run_event_digest(deps, DT))
    (event,) = deps.store.events.values()
    assert event["severity"] == 4 and event["status"] == "active"  # two independent sources
    auto = next(i for i in event["impacts"] if i["sector"] == "Auto")
    assert auto["basis"] == "llm_inference" and auto["magnitude"] == 0.4 and auto["confidence"] == 0.5
    assert [t["task_type"] for t in deps.store.hitl.values()] == ["high_severity_event"]
    assert out["triggers"] == [event["id"]]


def test_event_triggered_run_reviews_only_changed_recos():
    deps = fake_deps(l2_config())
    run(run_daily(deps, DT, horizons=["short"]))
    published = [r for r in deps.store.recos.values() if r["status"] == "published"]
    assert published
    triggers = run(run_event_digest(deps, DT))["triggers"]
    # The event hits Energy and Auto; force the re-evaluation to turn ONGC negative.
    deps.llm.hooks["LLM-07"] = lambda o, s: {**o, "score_adjustment": -1.0} if o["symbol"] == "ONGC" else o
    deps.llm.hooks["LLM-09"] = lambda o, s: {**o, "score_adjustment": -1.0} if o["symbol"] == "ONGC" else o
    deps.store.hitl.clear()
    out = run(run_event_triggered(deps, DT + timedelta(hours=1), triggers))
    reviewed = {deps.store.hitl[t]["payload"]["previous"]["symbol"] for t in out["tasks"]}
    assert reviewed == {"ONGC"}
    assert not any(r["status"] == "published" and r["run_type"] == "event_triggered" for r in deps.store.recos.values())


def test_portfolio_review_needs_evidence_for_thesis_broken():
    deps = fake_deps()
    out = run(run_portfolio_review(deps, DT, [1]))
    (pos,) = out["reviews"][0]["positions"]
    assert pos["status"] == "thesis_at_risk"
    assert [t["task_type"] for t in deps.store.hitl.values()] == ["review_thesis_break"]
