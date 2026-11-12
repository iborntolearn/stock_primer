"""Background jobs with progress events (on-demand analysis streams node-by-node progress over SSE)."""
from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

PROGRESS = {
    "resolve": "Finding the stock…", "context": "Reading the market regime…", "score": "Scoring with the quant engine…",
    "screen": "Screening the universe…", "research": "Research complete for one horizon",
    "collect": "Finishing up…",
}


@dataclass
class Job:
    id: int
    kind: str
    params: dict
    status: str = "running"            # running | succeeded | failed
    events: list[dict] = field(default_factory=list)
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(IST).isoformat())
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def emit(self, kind: str, message: str, **data: Any) -> None:
        self.events.append({"seq": len(self.events), "kind": kind, "message": message, **data})
        self.changed.set()

    def view(self) -> dict:
        return {"job_id": self.id, "kind": self.kind, "params": self.params, "status": self.status,
                "events": self.events, "result": self.result, "error": self.error, "created_at": self.created_at}


class JobManager:
    def __init__(self, max_jobs: int = 200):
        self._ids = itertools.count(1)
        self.jobs: dict[int, Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self.max_jobs = max_jobs

    def get(self, job_id: int) -> Optional[Job]:
        return self.jobs.get(job_id)

    def start_discover(self, deps, horizons: list[str], constraints: dict, max_ideas: int) -> Job:
        return self._start(deps, "discover", {"horizons": horizons, "constraints": constraints, "max_ideas": max_ideas})

    def start_chat(self, deps, message: str, history: list[str]) -> Job:
        return self._start(deps, "chat", {"message": message, "history": history[-6:]})

    def start_analysis(self, deps, query: str, horizons: list[str]) -> Job:
        return self._start(deps, "analyze", {"query": query, "horizons": horizons})

    def _start(self, deps, kind: str, params: dict) -> Job:
        job = Job(next(self._ids), kind, params)
        self.jobs[job.id] = job
        if len(self.jobs) > self.max_jobs:
            for old in sorted(self.jobs)[: len(self.jobs) - self.max_jobs]:
                self.jobs.pop(old, None)
        task = asyncio.create_task(self._run(job, deps))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job, deps) -> None:
        from orchestrator.graphs.on_demand import build_on_demand_graph
        from orchestrator.wiring import fresh_budget

        try:
            now = datetime.now(IST)
            fresh_budget(deps)
            p = job.params
            if job.kind == "chat":
                await self._chat(job, deps, now)
                return
            if job.kind == "analyze":
                trigger, label = f"analyze:{p['query']}:{','.join(p['horizons'])}", f"Analysing {p['query']}"
                state = {"mode": "analyze", "query": p["query"]}
            else:
                trigger, label = f"discover:{','.join(p['horizons'])}", "Looking for ideas"
                state = {"mode": "discover", "query": "", "constraints": p["constraints"], "max_ideas": p["max_ideas"]}
            run_id = deps.store.create_run("on_demand", now, trigger, deps.cfg.version,
                                           deps.cfg.strategy_version, deps.cfg.prompt_bundle_version)
            job.emit("started", label, run_id=run_id)
            final: dict = {}
            graph = build_on_demand_graph(deps)
            async for mode, chunk in graph.astream(
                    {"run_id": run_id, "decision_time": now.isoformat(), "horizons": p["horizons"], "results": [],
                     "errors": [], **state}, stream_mode=["updates", "values"]):
                if mode == "updates":
                    for node, update in (chunk or {}).items():
                        msg = PROGRESS.get(node, node)
                        if node == "research" and update and update.get("results"):
                            r = update["results"][0]
                            msg = (f"{r['horizon'].title()} horizon: {r['verdict']} ({r['status']})" if job.kind == "analyze"
                                   else f"{r['symbol']} {r['horizon']}: {r['verdict']} ({r['status']})")
                        job.emit("progress", msg, node=node)
                else:
                    final = chunk
            status = "succeeded" if final.get("results") or final.get("clarification") else "degraded"
            deps.store.finish_run(run_id, status, deps.budget.run_spent,
                                  {"errors": final.get("errors")} if final.get("errors") else None)
            job.result = {"run_id": run_id, "clarification": final.get("clarification"),
                          "results": final.get("results", []), "errors": final.get("errors", [])}
            job.status = "succeeded"
            job.emit("done", "Analysis complete" if job.kind == "analyze" else "Discovery complete", run_id=run_id)
        except Exception as e:
            log.exception("%s job %s failed", job.kind, job.id)
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
            job.emit("failed", job.error)


CHAT_PROGRESS = {"router": "Understanding the question…", "qa": "Looking things up…", "analyze": "Analysing the stock…",
                 "discover": "Screening for ideas…", "clarify": "Need a clarification", "unsupported": "Answering…"}


async def _chat_impl(job: Job, deps, now: datetime) -> None:
    from orchestrator.graphs.chat import build_chat_graph

    run_id = deps.store.create_run("on_demand", now, "chat", deps.cfg.version, deps.cfg.strategy_version,
                                   deps.cfg.prompt_bundle_version)
    job.emit("started", "Thinking", run_id=run_id)
    final: dict = {}
    async for mode, chunk in build_chat_graph(deps).astream(
            {"run_id": run_id, "decision_time": now.isoformat(), "message": job.params["message"],
             "history": job.params["history"]}, stream_mode=["updates", "values"]):
        if mode == "updates":
            for node in (chunk or {}):
                job.emit("progress", CHAT_PROGRESS.get(node, node), node=node)
        else:
            final = chunk
    deps.store.finish_run(run_id, "succeeded", deps.budget.run_spent)
    job.result = {"run_id": run_id, "intent": (final.get("route") or {}).get("intent"), "reply": final.get("reply", ""),
                  "citations": final.get("citations", []), "results": final.get("results", [])}
    job.status = "succeeded"
    job.emit("done", "Answered", run_id=run_id)


JobManager._chat = staticmethod(_chat_impl)
