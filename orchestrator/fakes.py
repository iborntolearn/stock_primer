"""In-memory stand-ins for the store, MCP tools, LLM and notifier.

Used by tests and by `stock-primer --dry-run` so the orchestrator can run end to
end before Postgres, the MCP servers and an API key are in place. The fake data
is synthetic and deterministic - it is not market data.
"""
from __future__ import annotations

import itertools
import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from pydantic import BaseModel

from .ports import EvidenceRow, M, ToolError, ToolGateway, ToolMeta, ToolResult


# --------------------------------------------------------------------------- store
class InMemoryStore:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.runs: dict[int, dict] = {}
        self.evidence: dict[int, EvidenceRow] = {}
        self.llm_calls: list[dict] = []
        self.security_events: list[dict] = []
        self.agent_outputs: list[dict] = []
        self.regimes: dict[str, dict] = {}
        self.events: dict[int, dict] = {}
        self.recos: dict[int, dict] = {}
        self.hitl: dict[int, dict] = {}

    def _id(self) -> int:
        return next(self._ids)

    def create_run(self, run_type, decision_time, trigger, config_version, strategy_version, prompt_bundle_version):
        rid = self._id()
        self.runs[rid] = {"id": rid, "run_type": run_type, "decision_time": decision_time, "trigger": trigger,
                          "status": "running", "config_version": config_version}
        return rid

    def finish_run(self, run_id, status, spent_usd, error=None):
        self.runs[run_id].update(status=status, spent_usd=spent_usd, error=error)

    def find_run(self, run_type, decision_date):
        return next((r["id"] for r in self.runs.values() if r["run_type"] == run_type
                     and r["decision_time"].date().isoformat() == decision_date), None)

    def add_evidence(self, kind: str, source: str, as_of: datetime, payload: Any, run_id: int) -> int:
        eid = self._id()
        self.evidence[eid] = EvidenceRow(eid, kind, source, as_of, payload, run_id)
        return eid

    def get_evidence(self, evidence_id):
        return self.evidence.get(evidence_id)

    def record_llm_call(self, row):
        self.llm_calls.append(row)

    def cached_llm_response(self, call_id, input_hash, max_age_hours):
        return None

    def record_security_event(self, row):
        self.security_events.append(row)

    def save_agent_output(self, run_id, agent, subject_type, subject_id, output, validation_status, confidence):
        self.agent_outputs.append({"run_id": run_id, "agent": agent, "subject_id": subject_id, "output": output})

    def save_regime(self, as_of, row):
        self.regimes[as_of] = row

    def latest_regime(self):
        return self.regimes[max(self.regimes)] if self.regimes else None

    def save_event(self, event):
        eid = self._id()
        self.events[eid] = {**event, "id": eid, "impacts": []}
        return eid

    def event_exists(self, event_key):
        return any(e["event_key"] == event_key for e in self.events.values())

    def save_event_impacts(self, event_id, impacts, run_id):
        self.events[event_id]["impacts"] = impacts["impacts"]

    def active_events(self, since):
        return [e for e in self.events.values() if e.get("status") in ("active", "unverified")]

    def save_recommendation(self, reco):
        rid = self._id()
        self.recos[rid] = {**reco, "id": rid}
        return rid

    def set_reco_status(self, reco_id, status, **fields):
        self.recos[reco_id].update(status=status, **fields)

    def recent_reco(self, symbol, horizon, since):
        rows = [r for r in self.recos.values() if r["symbol"] == symbol and r["horizon"] == horizon
                and r["status"] in ("published", "active") and r.get("published_at")
                and datetime.fromisoformat(r["published_at"]) >= since]
        return rows[-1] if rows else None

    def open_recos(self, sectors=None):
        return [r for r in self.recos.values() if r["status"] in ("published", "active")
                and (sectors is None or r.get("sector") in sectors)]

    def map_vocabulary(self):
        return (["crude_up", "gold_up", "monsoon_weak", "repo_up", "us10y_up", "usdinr_up"],
                sorted({u["sector"] for u in UNIVERSE.values()}))

    def approved_map_row(self, factor, sector):
        return {("crude_up", "Energy"): 1, ("crude_up", "Paints"): 2}.get((factor, sector))

    def save_map_proposal(self, p):
        pid = self._id()
        self.map_proposals = getattr(self, "map_proposals", {})
        self.map_proposals[pid] = dict(p)
        return pid

    def create_hitl_task(self, task_type, subject_type, subject_id, payload, ai_recommendation, reason,
                         priority=3, default_on_expiry="reject", due_at=None):
        tid = self._id()
        self.hitl[tid] = {"id": tid, "task_type": task_type, "subject_type": subject_type, "subject_id": subject_id,
                          "payload": payload, "ai_recommendation": ai_recommendation, "reason": reason,
                          "priority": priority, "status": "pending", "default_on_expiry": default_on_expiry}
        return tid


# --------------------------------------------------------------------------- tools
UNIVERSE = {
    "TATAMOTORS": {"sector": "Auto", "name": "Tata Motors", "close": 917.4, "score": 78.0},
    "HDFCBANK": {"sector": "Financials", "name": "HDFC Bank", "close": 1642.0, "score": 71.0},
    "INFY": {"sector": "IT", "name": "Infosys", "close": 1488.5, "score": 58.0},
    "ONGC": {"sector": "Energy", "name": "ONGC", "close": 262.3, "score": 69.0},
    "ASIANPAINT": {"sector": "Paints", "name": "Asian Paints", "close": 2875.0, "score": 41.0},
}

_KINDS = {
    "get_price_history": "price_series", "compute_indicators": "indicator", "get_index_history": "price_series",
    "get_stock_news": "news", "search_news": "news", "get_world_events": "event", "get_event": "event",
    "get_financials": "fundamental", "get_ratios": "fundamental", "get_valuation": "fundamental",
    "get_shareholding": "fundamental", "search_filings": "filing", "get_filing_excerpt": "filing",
    "get_macro_series": "macro", "get_flows": "flows", "get_regime_inputs": "macro",
}
_UNTRUSTED = {"get_stock_news", "search_news", "search_filings", "get_filing_excerpt"}


class FakeTools:
    """Implements the MCP tool catalogue (§5.4) over synthetic data."""

    def __init__(self, store: InMemoryStore, overrides: Optional[dict[str, Callable[[dict], Any]]] = None,
                 data_quality_passed: bool = True):
        self.store = store
        self.overrides = overrides or {}
        self.data_quality_passed = data_quality_passed
        self.calls: list[tuple[str, str]] = []

    async def call(self, role: str, tool: str, args: dict[str, Any], *, run_id: int, as_of: datetime) -> ToolResult:
        self.calls.append((role, tool))
        fn = self.overrides.get(tool) or getattr(self, f"_{tool}", None)
        if fn is None:
            return ToolResult(ok=False, error=ToolError("NOT_FOUND", f"unknown tool {tool}"))
        data = fn(args)
        if isinstance(data, ToolResult):
            return data
        kind = _KINDS.get(tool, "calc")
        src_as_of = as_of - timedelta(hours=3)
        eid = self.store.add_evidence(kind, "fake", src_as_of, data, run_id)
        return ToolResult(ok=True, data=data, meta=ToolMeta(evidence_id=eid, source="fake", as_of=src_as_of,
                                                            kind=kind, untrusted=tool in _UNTRUSTED))

    def tool_specs(self, names: frozenset[str]) -> list[dict[str, Any]]:
        return [{"name": n, "description": f"{n} (fake)", "input_schema": {"type": "object", "properties": {
            "symbol": {"type": "string"}}}} for n in sorted(names)]

    # --- quant / orchestrator tools
    def _data_quality_status(self, a):
        return {"passed": self.data_quality_passed, "is_trading_day": True,
                "issues": [] if self.data_quality_passed else ["freshness 91% < 98%"]}

    def _quant_row(self, symbol: str, horizon: str) -> dict:
        u = UNIVERSE[symbol]
        return {"symbol": symbol, "sector": u["sector"], "quant_score": u["score"], "confidence": 0.7,
                "gates": {"liquidity": True, "price_floor": True, "regime": True}, "gates_passed": True,
                "rank": sorted(UNIVERSE, key=lambda s: -UNIVERSE[s]["score"]).index(symbol) + 1,
                "above_200dma": True, "atr": round(u["close"] * 0.02, 2), "sector_index": f"NIFTY_{u['sector'].upper()}"}

    def _screen_universe(self, a):
        rows = [self._quant_row(s, a["horizon"]) for s in UNIVERSE]
        rows = sorted(rows, key=lambda r: -r["quant_score"])[: a.get("top_k", 10)]
        if a.get("constraints", {}).get("sector"):
            rows = [r for r in rows if r["sector"] == a["constraints"]["sector"]]
        return {"candidates": rows}

    def _score_horizon(self, a):
        return self._quant_row(a["symbol"], a["horizon"])

    def _propose_levels(self, a):
        c = UNIVERSE[a["symbol"]]["close"]
        atr = c * 0.02
        stop_mult, rr = {"short": (1.75, 2.0), "medium": (3.0, 2.5), "long": (6.0, 2.0)}[a["horizon"]]
        entry_low, entry_high = round(c - 0.5 * atr, 2), round(c + 0.5 * atr, 2)
        stop = round(entry_low - stop_mult * atr, 2)
        target = round(entry_high + rr * (entry_high - stop), 2)
        return {"entry_low": entry_low, "entry_high": entry_high, "stop": stop, "target": target,
                "rr": round((target - entry_high) / (entry_high - stop), 2), "method": "atr"}

    def _resolve_instrument(self, a):
        q = a["query"].upper()
        hits = [{"symbol": s, "name": u["name"], "sector": u["sector"], "confidence": 0.95}
                for s, u in UNIVERSE.items() if q in s or q in u["name"].upper()]
        return {"candidates": hits}

    # --- market / macro
    def _get_regime_inputs(self, a):
        return {"quant_label": "neutral", "score": 0.12, "nifty_trend": "up", "breadth_pct_above_200dma": 58.0,
                "india_vix": 14.2, "fii_5d_cr": -1200.0, "usdinr_chg_5d_pct": 0.3, "brent_chg_5d_pct": 3.1,
                "us10y": 4.21}

    def _get_market_breadth(self, a):
        return {"advances": 1210, "declines": 870, "pct_above_50dma": 61.0, "pct_above_200dma": 58.0}

    def _get_flows(self, a):
        return {"rows": [{"date": "2026-10-12", "fii_net": -540.0, "dii_net": 910.0}]}

    def _get_index_history(self, a):
        return {"index_code": a.get("index_code"), "ret_1m_pct": 2.4, "ret_3m_pct": 5.1, "last": 24012.5}

    def _get_macro_series(self, a):
        return {"series": a.get("series_id"), "BRENT_chg_5d_pct": 3.1, "USDINR_last": 83.92}

    def _get_sector_sensitivities(self, a):
        return {"rows": [{"id": 1, "factor": "crude_up", "sector": "Energy", "direction": 1, "magnitude": 0.6},
                         {"id": 2, "factor": "crude_up", "sector": "Paints", "direction": -1, "magnitude": 0.5}]}

    # --- stock research
    def _compute_indicators(self, a):
        c = UNIVERSE[a["symbol"]]["close"]
        return {"symbol": a["symbol"], "close": c, "RSI14": 61.3, "ATR14": round(c * 0.02, 2),
                "SMA50": round(c * 0.96, 2), "SMA200": round(c * 0.9, 2), "VOL_RATIO20": 1.8, "BREAKOUT20": True}

    def _get_price_history(self, a):
        c = UNIVERSE[a["symbol"]]["close"]
        return {"symbol": a["symbol"], "rows": [{"date": "2026-10-13", "close": c}], "hi52": round(c * 1.08, 2),
                "lo52": round(c * 0.72, 2)}

    def _get_ratios(self, a):
        return {"symbol": a["symbol"], "roce": 18.4, "roe": 16.1, "de": 0.4, "sector_median_roe": 14.0}

    def _get_valuation(self, a):
        return {"symbol": a["symbol"], "pe": 24.5, "sector_pe": 27.0, "pe_5y_pct": 40}

    def _get_financials(self, a):
        return {"symbol": a["symbol"], "quarters": [{"period_end": "2026-06-30", "revenue": 10234.0, "pat": 1210.0,
                                                     "published_at": "2026-08-05"}]}

    def _get_shareholding(self, a):
        return {"symbol": a["symbol"], "promoter_pct": 46.4, "pledged_pct": 0.0}

    def _search_filings(self, a):
        return {"symbol": a["symbol"], "chunks": [{"doc_ref": "Q1FY27", "text": "No auditor qualifications."}]}

    def _get_stock_news(self, a):
        return {"symbol": a["symbol"], "items": [
            {"news_id": 101, "title": f"{a['symbol']} wins large order", "source": "exchange filing",
             "published_at": "2026-10-10T10:00:00+05:30", "snippet": "Order worth disclosed in filing."}]}

    def _get_event_calendar(self, a):
        return {"symbol": a.get("symbol"), "items": [{"what": "Q2 results", "date": "2026-10-24"}]}

    def _get_corporate_actions(self, a):
        return {"symbol": a.get("symbol"), "items": []}

    def _search_news(self, a):
        return {"items": [
            {"news_id": 201, "title": "Brent jumps on supply concerns", "source": "wire", "snippet": "Crude up 3%",
             "published_at": "2026-10-13T08:00:00+05:30"},
            {"news_id": 202, "title": "Oil rallies as output cut extended", "source": "paper", "snippet": "Brent higher",
             "published_at": "2026-10-13T09:00:00+05:30"}]}

    def _get_world_news(self, a):
        return self._search_news(a)

    def _get_event(self, a):
        return {"event_id": a.get("event_id")}

    def _get_portfolio(self, a):
        return {"portfolio_id": a["portfolio_id"], "positions": [{"symbol": "INFY", "qty": 10, "avg_cost": 1450.0,
                                                                   "thesis": "Deal wins", "stop": 1380.0}]}

    def _get_exposure(self, a):
        return {"by_sector": {"IT": 0.4}}


# --------------------------------------------------------------------------- llm
_IDS = re.compile(r'id="(E\d+)"')


class ScriptedLLM:
    """Deterministic LLM double. Produces schema-valid, grounded outputs from the prompt inputs.

    `hooks[call_id](default_dict, sections) -> dict` can rewrite an output to test failure paths.
    """

    def __init__(self, hooks: Optional[dict[str, Callable[[dict, dict], dict]]] = None,
                 fail: Optional[set[str]] = None, record_to=None):
        self.hooks = hooks or {}
        self.fail = fail or set()
        self.calls: list[str] = []
        self.record_to = record_to   # a Store: write llm_call rows like ClaudeLLM does (enables replay tests)

    async def structured(self, *, call_id, agent, run_id, decision_time, sections, output_model: type[M],
                         cite_ids=frozenset(), template_vars=None, images=None) -> M:
        from .llm import LLMCallFailed

        self.calls.append(call_id)
        if call_id in self.fail:
            raise LLMCallFailed(call_id, "scripted failure")
        ids = sorted(cite_ids) or _IDS.findall(" ".join(sections.values()))
        out = getattr(self, f"_{call_id.replace('-', '_')}")(sections, ids, template_vars or {})
        if call_id in self.hooks:
            out = self.hooks[call_id](out, sections)
        parsed = output_model.model_validate(out)
        if self.record_to is not None:
            from .config import Config
            from .llm import render_request

            version, _, data, h = render_request(Config(), call_id, decision_time, sections, template_vars)
            self.record_to.record_llm_call({
                "run_id": run_id, "agent": agent, "call_id": call_id, "model_id": "scripted", "prompt_version": version,
                "input_hash": h, "request": {"data": data}, "response": parsed.model_dump(mode="json"), "tokens_in": 0,
                "tokens_out": 0, "cost_usd": 0.0, "latency_ms": 0, "validation_status": "passed", "retries": 0})
        return parsed

    async def tool_answer(self, *, call_id, agent, run_id, decision_time, question, tools, role):
        self.calls.append(call_id)
        r = await tools.call(role, "get_regime_inputs", {}, run_id=run_id, as_of=decision_time)
        ref = r.evidence_ref
        return f"The market regime is neutral [{ref}]. Decision-support only.", [ref]

    # one builder per call id
    def _LLM_01(self, s, ids, v):
        msg = s["message"].upper()
        sym = next((k for k in UNIVERSE if k in msg), None)
        if sym:
            return {"intent": "analyze_stock", "symbols": [sym], "horizon": "all", "constraints": {},
                    "needs_clarification": False}
        if "LOOK AT" in msg or "BUY" in msg:
            return {"intent": "discover_ideas", "symbols": [], "horizon": None, "constraints": {"max_ideas": 2},
                    "needs_clarification": False}
        return {"intent": "market_context", "symbols": [], "horizon": None, "constraints": {},
                "needs_clarification": False}

    def _LLM_02(self, s, ids, v):
        news_ids = [int(x) for x in re.findall(r'id="N(\d+)"', s["articles"])]
        return {"events": [{"event_key": "commodity-global-brent-2026-10-13", "event_type": "commodity",
                            "title": "Brent rises on supply concerns", "summary": "Crude prices rose.",
                            "geographies": ["GLOBAL"], "severity": 4, "severity_rationale": "Broad crude move",
                            "market_relevance_to_india": "high",
                            "transmission_hypotheses": [{"channel": "crude", "description": "Input costs"}],
                            "source_news_ids": news_ids, "confidence": 0.7}]}

    def _LLM_03(self, s, ids, v):
        return {"event_id": 0, "impacts": [
            {"sector": "Energy", "direction": 1, "magnitude": 0.6, "horizon": "short", "channel": "crude",
             "rationale": "Upstream realisations", "basis": "approved_map", "map_row_ids": [1],
             "evidence_ids": ids[:1], "confidence": 0.7},
            {"sector": "Auto", "direction": -1, "magnitude": 0.7, "horizon": "short", "channel": "crude",
             "rationale": "Fuel costs", "basis": "llm_inference", "map_row_ids": [], "evidence_ids": ids[:1],
             "confidence": 0.8}], "stock_specific": [], "decay_days": 10, "data_gaps": []}

    def _LLM_04(self, s, ids, v):
        label = s["quant_label"]
        return {"regime_label_confirmed": label, "agrees_with_quant_label": True,
                "drivers": [{"factor": "breadth", "reading": "healthy", "evidence_ids": ids[:1]}],
                "contradictions": ["FII selling while DII buying"], "narrative": f"Regime is {label}.",
                "implications": {"short": "selective", "medium": "neutral", "long": "accumulate quality"}}

    def _LLM_05(self, s, ids, v):
        return {"leading_sectors": [{"sector": "Energy", "reason": "crude", "evidence_ids": ids[:1], "confidence": 0.6}],
                "lagging_sectors": [{"sector": "Paints", "reason": "input costs", "evidence_ids": ids[:1]}],
                "themes": [], "sector_overlay": [{"sector": "Energy", "overlay_score": 0.4},
                                                 {"sector": "Paints", "overlay_score": -0.4}]}

    def _LLM_06(self, s, ids, v):
        syms = re.findall(r'<stock symbol="([A-Z&-]+)">', s["candidates"])
        return {"items": [{"symbol": x, "keep": True, "red_flags": ["none"], "reason": "no red flags"} for x in syms]}

    def _LLM_07(self, s, ids, v):
        return {"symbol": v["symbol"], "horizon": v["horizon"], "trend": "up", "setup": "breakout",
                "supporting_points": [{"point": "Breakout on above-average volume", "evidence_ids": ids[:1]}],
                "concerns": [], "key_levels": {"support": [], "resistance": [], "evidence_ids": ids[:1]},
                "stance": "constructive", "score_adjustment": 0.4, "confidence": 0.7, "data_gaps": []}

    def _LLM_08(self, s, ids, v):
        pt = [{"point": "Returns above sector median", "evidence_ids": ids[:1]}]
        return {"symbol": v["symbol"], "horizon": v["horizon"], "quality": {"assessment": "strong", "points": pt},
                "growth": {"assessment": "adequate", "points": pt},
                "valuation": {"assessment": "fair", "vs": "sector", "points": pt},
                "balance_sheet_and_governance": {"flags": []}, "earnings_trajectory": "stable",
                "score_adjustment": 0.2, "confidence": 0.6, "data_gaps": []}

    def _LLM_09(self, s, ids, v):
        return {"symbol": v["symbol"], "recent_developments": [
            {"summary": "Order win disclosed", "type": "order_win", "impact": "positive", "materiality": "med",
             "evidence_ids": ids[:1]}], "upcoming_catalysts": [{"what": "Q2 results", "date": "2026-10-24",
                                                                 "evidence_ids": ids[-1:]}],
                "sentiment": 0.3, "event_exposure": [], "score_adjustment": 0.2, "confidence": 0.6}

    def _LLM_10(self, s, ids, v):
        q = json.loads(s["quant"])
        return {"symbol": q["symbol"], "horizon": q["horizon"], "verdict": "proceed_with_caution",
                "strongest_counterarguments": [{"argument": "Results fall inside the holding window",
                                                "severity": "med", "evidence_ids": ids[-1:]}],
                "unsupported_claims": [], "inconsistencies": [], "missing_checks": [],
                "base_rate_note": "Breakouts fail often", "confidence_penalty": 0.05, "score_penalty": 0.2,
                "invalidation_conditions_suggested": [{"condition": "Close below stop", "machine_checkable": True,
                                                       "metric": "close", "threshold": "stop"}]}

    def _LLM_11(self, s, ids, v):
        q = json.loads(s["quant"])
        lv = q["levels"]
        verdict = q["verdict"]
        action = {"favourable": "accumulate", "neutral": "watch", "avoid": "avoid"}[verdict]
        return {"symbol": q["symbol"], "horizon": q["horizon"], "verdict": verdict, "action": action,
                "headline": f"{q['symbol']} setup with sector support", "thesis": "Breakout with volume and order win.",
                "bull_case": ["Breakout confirmed by volume"], "bear_case": ["Results inside the window"],
                "key_risks": [{"risk": "Earnings event", "evidence_ids": ids[-1:]}],
                "what_to_watch": ["Volume follow-through"],
                "levels": {k: lv[k] for k in ("entry_low", "entry_high", "stop", "target")},
                "invalidation": [{"condition": "Close below stop", "machine_checkable": True, "metric": "close",
                                  "threshold": str(lv["stop"])}],
                "confidence": q["max_confidence"], "confidence_rationale": "Quant confidence less critic penalty",
                "claims": [{"text": "Price broke out on above-average volume", "evidence_ids": ids[:1]},
                           {"text": f"Entry zone {lv['entry_low']} to {lv['entry_high']}", "evidence_ids": ids[:1]}],
                "disclaimer_key": "decision_support_v1"}

    def _LLM_12(self, s, ids, v):
        return {"portfolio_id": 1, "as_of": "2026-10-13", "positions": [
            {"symbol": "INFY", "status": "thesis_broken", "changes_since_entry": [],
             "suggested_action": "exit_candidate", "reason": "Momentum faded"}],
                "concentration_flags": [], "event_exposure": [], "summary": "One position needs review."}

    def _LLM_13(self, s, ids, v):
        events = [e["event_id"] for e in json.loads(s["recent_events"])]
        sectors = json.loads(s["sectors"])
        return {"proposals": [
            {"action": "modify", "factor": "crude_up", "sector": sectors[0], "direction": -1, "magnitude": 0.95,
             "rationale": "Repeated crude spikes hit this sector harder than mapped", "supporting_event_ids": events,
             "supporting_outcome_ids": [], "expected_confidence": 0.7},
            {"action": "add", "factor": "crude_up", "sector": "Not A Sector", "direction": 1, "magnitude": 0.3,
             "rationale": "x", "supporting_event_ids": events, "supporting_outcome_ids": [], "expected_confidence": 0.5},
            {"action": "add", "factor": "usdinr_up", "sector": sectors[-1], "direction": 1, "magnitude": 0.3,
             "rationale": "Thin evidence", "supporting_event_ids": events[:1], "supporting_outcome_ids": [],
             "expected_confidence": 0.4}]}

    def _LLM_15(self, s, ids, v):
        line = lambda x: {"value": x, "page": 1}
        return {"period_end": v["period_end"], "nature": "consolidated", "currency_unit": "INR_crore",
                "revenue": line(1000.0), "other_income": line(50.0), "total_income": line(1050.0),
                "profit_before_tax": line(200.0), "tax_expense": line(50.0), "net_profit": line(150.0),
                "net_profit_owners": line(150.0), "eps_basic": line(15.0), "paid_up_capital": line(100.0),
                "face_value": line(10.0), "extraction_confidence": 0.92, "unreadable_fields": []}

    def _LLM_16(self, s, ids, v):
        cards = json.loads(s["cards"])
        return {"attention": json.loads(s["attention_items"]), "market": "Regime neutral.",
                "picks": [{"symbol": c["symbol"], "horizon": c["horizon"], "lines": c["headline"]} for c in cards],
                "no_pick_notes": [f"{h}: {r}" for h, r in json.loads(s["no_pick_reasons"]).items()]}


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, channel: str, kind: str, text: str, payload: Optional[dict] = None) -> None:
        self.sent.append({"channel": channel, "kind": kind, "text": text, "payload": payload})


def fake_deps(cfg=None, *, llm=None, tools_overrides=None, data_quality_passed=True):
    """Wire up a Deps object entirely from fakes."""
    from .budget import BudgetManager
    from .config import Config
    from .deps import Deps
    from .ports import AllowlistedTools

    cfg = cfg or Config()
    store = InMemoryStore()
    raw = FakeTools(store, tools_overrides, data_quality_passed)
    return Deps(cfg=cfg, tools=AllowlistedTools(raw), store=store, llm=llm or ScriptedLLM(),
                notifier=RecordingNotifier(),
                budget=BudgetManager(cfg.budget_usd_per_run, cfg.budget_usd_per_day, cfg.budget_soft_fraction))
