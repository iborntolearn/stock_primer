"""Replay a stored run from its evidence and recorded LLM responses (§1.6 reproducibility, §13 replay tests).

Tools are answered from the run's tool_call -> evidence rows (matched by tool and
argument hash); LLM calls from llm_call rows (matched by call id and input hash).
A replay therefore needs no network and costs nothing. If the code now asks a
question the original run never asked, replay reports it as a divergence.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m
from mcp_servers.service import args_hash

from .config import Config
from .llm import LLMCallFailed, question_hash, render_request
from .ports import M, ToolError, ToolGateway, ToolMeta, ToolResult


IST = timezone(timedelta(hours=5, minutes=30))


class ReplayDivergence(Exception):
    pass


class ReplayToolGateway:
    def __init__(self, factory: sessionmaker[Session], run_id: int, specs_from: Optional[ToolGateway] = None):
        self.f = factory
        self.run_id = run_id
        self.specs_from = specs_from
        self.misses: list[str] = []

    def tool_specs(self, names):
        return self.specs_from.tool_specs(names) if self.specs_from else []

    async def call(self, role: str, tool: str, args: dict[str, Any], *, run_id: int, as_of: datetime) -> ToolResult:
        h = args_hash(tool, args)
        with self.f() as s:
            tc = s.scalar(select(m.ToolCall).where(m.ToolCall.run_id == self.run_id, m.ToolCall.tool == tool,
                                                   m.ToolCall.args_hash == h).order_by(m.ToolCall.id).limit(1))
            if tc is None:
                self.misses.append(f"{tool} {json.dumps({k: v for k, v in args.items() if k != 'as_of'}, default=str)[:120]}")
                return ToolResult(ok=False, error=ToolError("NOT_FOUND", "not in recorded run"))
            if not tc.ok or tc.result_ref is None:
                return ToolResult(ok=False, error=ToolError(tc.error_code or "UNKNOWN", "recorded failure"))
            e = s.get(m.Evidence, tc.result_ref)
        data = e.payload.get("value") if set(e.payload) == {"value"} else e.payload
        return ToolResult(ok=True, data=data, meta=ToolMeta(evidence_id=e.id, source=e.source, as_of=e.as_of, kind=e.kind,
                                                            untrusted=e.kind in ("news", "filing")))


class ReplayLLM:
    def __init__(self, cfg: Config, factory: sessionmaker[Session], run_id: int):
        self.cfg, self.f, self.run_id = cfg, factory, run_id
        self.misses: list[str] = []

    def _response(self, call_id: str, input_hash: str) -> Optional[dict]:
        with self.f() as s:
            row = s.scalar(select(m.LlmCall).where(m.LlmCall.run_id == self.run_id, m.LlmCall.call_id == call_id,
                                                   m.LlmCall.input_hash == input_hash).limit(1))
        return row.response if row is not None and row.validation_status != "failed" else None

    async def structured(self, *, call_id, agent, run_id, decision_time, sections, output_model: type[M],
                         cite_ids=frozenset(), template_vars=None, images=None) -> M:
        _, _, _, h = render_request(self.cfg, call_id, decision_time, sections, template_vars, images)
        resp = self._response(call_id, h)
        if resp is None:
            self.misses.append(f"{call_id}/{agent}")
            raise LLMCallFailed(call_id, "not in recorded run (prompt or inputs changed)")
        return output_model.model_validate(resp)

    async def tool_answer(self, *, call_id, agent, run_id, decision_time, question, tools, role):
        h = question_hash(call_id, self.cfg.prompt_versions.get(call_id, "v1"), decision_time, question)
        resp = self._response(call_id, h)
        if resp is None:
            self.misses.append(call_id)
            raise LLMCallFailed(call_id, "not in recorded run")
        return resp["answer"], []


def compare_results(original: list[dict], replayed: list[dict]) -> list[str]:
    """Levels and verdicts must match exactly; confidence within 0.01."""
    diffs = []
    key = lambda r: (r["symbol"], r["horizon"])
    o, n = {key(r): r for r in original}, {key(r): r for r in replayed}
    for k in sorted(set(o) | set(n)):
        a, b = o.get(k), n.get(k)
        if a is None or b is None:
            diffs.append(f"{k}: only in {'replay' if a is None else 'original'}")
            continue
        for f in ("verdict", "status", "entry_low", "entry_high", "stop", "target"):
            if a.get(f) != b.get(f):
                diffs.append(f"{k}: {f} {a.get(f)} -> {b.get(f)}")
        if abs((a.get("confidence") or 0) - (b.get("confidence") or 0)) > 0.01:
            diffs.append(f"{k}: confidence {a.get('confidence')} -> {b.get('confidence')}")
    return diffs


async def replay_analysis(factory: sessionmaker[Session], cfg: Config, run_id: int, notifier=None) -> dict[str, Any]:
    """Re-run a recorded on-demand analysis and compare levels/verdicts with the original."""
    from core.store import SqlStore

    from .budget import BudgetManager
    from .deps import Deps
    from .graphs.on_demand import analyze
    from .ports import AllowlistedTools
    from .wiring import ConsoleNotifier

    with factory() as s:
        run = s.get(m.Run, run_id)
        if run is None or not (run.trigger or "").startswith("analyze:"):
            raise ValueError(f"run {run_id} is not a recorded on-demand analysis")
        _, query, horizons = run.trigger.split(":", 2)
        original = []
        for r, sym in s.execute(select(m.Recommendation, m.Instrument.symbol).join(m.Instrument)
                                .where(m.Recommendation.run_id == run_id)):
            original.append({"symbol": sym, "horizon": r.horizon, "verdict": r.verdict, "status": r.status,
                             "confidence": float(r.confidence or 0), **{k: float(getattr(r, k)) if getattr(r, k) is not None else None
                                                                        for k in ("entry_low", "entry_high", "stop", "target")}})
        # Stored in UTC; agents and prompts use IST, so restore the original offset for identical prompts.
        decision_time = run.decision_time.astimezone(IST)
    store = SqlStore(factory)
    tools, llm = ReplayToolGateway(factory, run_id), ReplayLLM(cfg, factory, run_id)
    deps = Deps(cfg, AllowlistedTools(tools), store, llm, notifier or ConsoleNotifier(), BudgetManager(1e9, 1e9))
    out = await analyze(deps, query, decision_time, horizons.split(","))
    replayed = [{"symbol": r["symbol"], "horizon": r["horizon"], "verdict": r["verdict"], "status": r["status"],
                 "confidence": r["confidence"],
                 **{k: (r["card"] or {}).get("levels", {}).get(k) if r.get("card") else None
                    for k in ("entry_low", "entry_high", "stop", "target")}} for r in out.get("results", [])]
    # Levels for rejected cards come from the recommendation row, not the card.
    with factory() as s:
        for rr in replayed:
            row = s.scalar(select(m.Recommendation).join(m.Instrument).where(
                m.Recommendation.run_id == out["run_id"], m.Instrument.symbol == rr["symbol"],
                m.Recommendation.horizon == rr["horizon"]))
            for k in ("entry_low", "entry_high", "stop", "target"):
                rr[k] = float(getattr(row, k)) if getattr(row, k) is not None else None
    return {"original_run": run_id, "replay_run": out["run_id"], "diffs": compare_results(original, replayed),
            "tool_misses": tools.misses, "llm_misses": llm.misses}
