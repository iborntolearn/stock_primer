"""FastAPI app (§4.7).

    uvicorn api.main:app --port 8000          # or: python -m api.main

Conventions: JSON; ISO-8601 timestamps; errors as {code, message, details, retryable};
POSTs that create trades need an Idempotency-Key header.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from core import models as m
from core.settings import get_settings
from evaluation.scorecard import scorecard
from hitl.service import DecisionError, HitlService
from simulator.engine import OrderRejected, Simulator
from simulator.metrics import performance

from .jobs import JobManager

IST = timezone(timedelta(hours=5, minutes=30))


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None, retryable: bool = False):
        self.status, self.code, self.message, self.details, self.retryable = status, code, message, details, retryable


def _f(x) -> Optional[float]:
    return None if x is None else float(x)


# ---------------------------------------------------------------- request bodies
class AnalyzeIn(BaseModel):
    symbol: str
    horizons: list[str] = ["short", "medium", "long"]


class PortfolioIn(BaseModel):
    name: str
    starting_cash: float = Field(gt=0)
    type: str = Field(default="manual", pattern="^(manual|auto_follow)$")
    benchmark: str = "NIFTY 500"
    rules: dict[str, Any] = {}


class OrderIn(BaseModel):
    symbol: Optional[str] = None
    reco_id: Optional[int] = None
    amount: Optional[float] = Field(default=None, gt=0)
    qty: Optional[int] = Field(default=None, gt=0)
    stop: Optional[float] = None
    target: Optional[float] = None
    horizon: Optional[str] = None


class DecisionIn(BaseModel):
    action: str
    note: Optional[str] = None
    edits: dict[str, Any] = {}


class KillSwitchIn(BaseModel):
    enabled: bool


class AutonomyIn(BaseModel):
    action: str = Field(pattern="^publish_(short|medium|long)$")
    level: int = Field(ge=0, le=3)
    note: Optional[str] = None


class AlertsIn(BaseModel):
    enabled: bool
    stop_proximity_pct: float = Field(default=1.5, gt=0, le=10)


class ShockIn(BaseModel):
    factor: str
    change: float


class StressIn(BaseModel):
    preset: Optional[str] = None
    text: Optional[str] = Field(default=None, max_length=500)
    shocks: list[ShockIn] = []
    portfolio_ids: list[int] = []


class ReplayIn(BaseModel):
    days: int = Field(default=20, ge=5, le=120)
    start: Optional[date] = None
    end: Optional[date] = None
    portfolio_ids: list[int] = []


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[str] = []


class DiscoverIn(BaseModel):
    horizons: list[str] = ["short", "medium"]
    sector: Optional[str] = None
    max_ideas: int = Field(default=5, ge=1, le=10)


# ---------------------------------------------------------------- app factory
def create_app(deps_cm_factory=None, factory=None) -> FastAPI:
    """deps_cm_factory() -> async context manager yielding Deps (defaults to orchestrator.wiring.real_deps)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from core.db import session_factory

        app.state.f = factory or session_factory()
        if deps_cm_factory is None:
            from orchestrator.wiring import real_deps

            cm = real_deps(llm="live", factory=app.state.f)
        else:
            cm = deps_cm_factory()
        app.state.deps = await cm.__aenter__()
        app.state.sim = Simulator(app.state.f)
        app.state.hitl = HitlService(app.state.f, app.state.deps.cfg.rr_floor)
        app.state.jobs = JobManager()
        try:
            yield
        finally:
            await cm.__aexit__(None, None, None)

    app = FastAPI(title="stock_primer API", version="0.2", lifespan=lifespan)
    settings = get_settings()
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                       allow_headers=["*"], allow_credentials=False)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, e: ApiError):
        return JSONResponse(status_code=e.status, content={"code": e.code, "message": e.message,
                                                           "details": e.details, "retryable": e.retryable})

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, e: HTTPException):
        return JSONResponse(status_code=e.status_code, content={"code": "HTTP_ERROR", "message": str(e.detail),
                                                                "details": None, "retryable": False})

    def auth(request: Request, authorization: Optional[str] = Header(default=None)) -> None:
        """Bearer token (scripts) or the login session cookie (browser). Open only when neither is configured."""
        from . import auth as login

        st = get_settings()
        if st.api_token and authorization == f"Bearer {st.api_token}":
            return
        if login.configured(st):
            if login.valid_session(request.cookies.get(login.COOKIE), st.auth_session_key):
                return
            raise ApiError(401, "LOGIN_REQUIRED", "sign in again")
        if st.api_token:
            raise ApiError(401, "UNAUTHORIZED", "missing or wrong bearer token")

    def record_security(flag: str, source: str) -> None:
        try:
            with app.state.f() as s, s.begin():
                s.add(m.SecurityEvent(agent="login", source=source[:100], flag=flag,
                                      action_taken="locked 15 min" if flag == "login_locked" else None))
        except Exception:
            pass                                       # never block a login on audit storage

    from .auth import router as login_router

    app.include_router(login_router(get_settings, record_security))

    def db(request: Request):
        with request.app.state.f() as s:
            yield s

    api = "/api/v1"
    protected = [Depends(auth)]

    # -- health / digest / market ----------------------------------------------------
    @app.get(f"{api}/health")
    def health(s=Depends(db)):
        gate = s.scalar(select(m.DataGateResult).order_by(m.DataGateResult.decision_date.desc()).limit(1))
        return {"status": "ok", "last_price_date": str(s.scalar(select(func.max(m.PriceDaily.trade_date)))),
                "last_gate": {"date": gate.decision_date.isoformat(), "passed": gate.passed} if gate else None,
                "kill_switch": app.state.deps.cfg.kill_switch, "last_backup": _last_backup()}

    @app.get(f"{api}/digest", dependencies=protected)
    def digest(date_: Optional[date] = Query(default=None, alias="date"), s=Depends(db)):
        q = select(m.AgentOutput).where(m.AgentOutput.agent == "digest")
        if date_:
            q = q.where(m.AgentOutput.subject_id == date_.isoformat())
        row = s.scalar(q.order_by(m.AgentOutput.id.desc()).limit(1))
        if row is None:
            raise ApiError(404, "NOT_FOUND", "no digest yet")
        return {"date": row.subject_id, **row.output}

    @app.get(f"{api}/market/regime", dependencies=protected)
    def regime(request: Request):
        r = request.app.state.deps.store.latest_regime()
        if r is None:
            raise ApiError(404, "NOT_FOUND", "no regime snapshot yet")
        return r

    @app.get(f"{api}/market/events", dependencies=protected)
    def events(request: Request, days: int = 14):
        return {"events": request.app.state.deps.store.active_events(datetime.now(timezone.utc) - timedelta(days=days))}

    # -- recommendations ---------------------------------------------------------------
    def reco_row(s, r: m.Recommendation, sym: str) -> dict:
        o = s.get(m.Outcome, r.id)
        return {"id": r.id, "symbol": sym, "horizon": r.horizon, "verdict": r.verdict, "action": r.action,
                "status": r.status, "quant_score": _f(r.quant_score), "final_score": _f(r.final_score),
                "confidence": _f(r.confidence), "entry_low": _f(r.entry_low), "entry_high": _f(r.entry_high),
                "stop": _f(r.stop), "target": _f(r.target), "risk_reward": _f(r.risk_reward),
                "headline": (r.card or {}).get("headline"), "run_id": r.run_id,
                "run_type": (r.meta or {}).get("run_type"),
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "closed_at": r.closed_at.isoformat() if r.closed_at else None, "close_reason": r.close_reason,
                "outcome": {"ret_pct": _f(o.ret_pct), "alpha_pct": _f(o.alpha_pct), "hit_target": o.hit_target,
                            "hit_stop": o.hit_stop, "days_held": o.days_held} if o else None}

    @app.get(f"{api}/recommendations", dependencies=protected)
    def recommendations(horizon: Optional[str] = None, status: Optional[str] = None, symbol: Optional[str] = None,
                        limit: int = Query(50, le=200), offset: int = 0, s=Depends(db)):
        q = select(m.Recommendation, m.Instrument.symbol).join(m.Instrument)
        if horizon:
            q = q.where(m.Recommendation.horizon == horizon)
        if status:
            q = q.where(m.Recommendation.status.in_(status.split(",")))
        if symbol:
            q = q.where(m.Instrument.symbol == symbol.upper())
        rows = s.execute(q.order_by(m.Recommendation.id.desc()).offset(offset).limit(limit)).all()
        return {"items": [reco_row(s, r, sym) for r, sym in rows]}

    @app.get(f"{api}/recommendations/{{reco_id}}", dependencies=protected)
    def recommendation(reco_id: int, s=Depends(db)):
        r = s.get(m.Recommendation, reco_id)
        if r is None:
            raise ApiError(404, "NOT_FOUND", f"recommendation {reco_id}")
        sym = s.get(m.Instrument, r.instrument_id).symbol
        ev = s.execute(select(m.Evidence, m.RecoEvidence.role).join(m.RecoEvidence, m.RecoEvidence.evidence_id == m.Evidence.id)
                       .where(m.RecoEvidence.reco_id == reco_id)).all()
        run = s.get(m.Run, r.run_id)
        agents = {}
        for a in s.scalars(select(m.AgentOutput).where(m.AgentOutput.run_id == r.run_id, m.AgentOutput.subject_id == sym,
                                                       m.AgentOutput.agent.in_(("technical", "fundamental", "news", "critic")))
                           .order_by(m.AgentOutput.id)):
            if a.output.get("horizon") in (None, r.horizon):
                agents[a.agent] = a.output
        return {**reco_row(s, r, sym), "card": r.card, "agents": agents,
                "contributions": (r.meta or {}).get("contributions", []), "thesis": r.thesis, "bull_case": r.bull_case,
                "bear_case": r.bear_case, "key_risks": r.key_risks, "invalidation": r.invalidation,
                "regime_at_issue": r.regime_at_issue, "violations": (r.meta or {}).get("violations", []),
                "gaps": (r.meta or {}).get("gaps", []), "critique": (r.meta or {}).get("critique"),
                "evidence": [{"id": f"E{e.id}", "kind": e.kind, "source": e.source, "as_of": e.as_of.isoformat(),
                              "role": role, "source_ref": e.source_ref} for e, role in ev],
                "run": {"id": run.id, "run_type": run.run_type, "decision_time": run.decision_time.isoformat(),
                        "config_version": run.config_version, "prompt_bundle_version": run.prompt_bundle_version,
                        "strategy_version": run.strategy_version} if run else None}

    @app.get(f"{api}/evidence/{{evidence_id}}", dependencies=protected)
    def evidence(evidence_id: str, s=Depends(db)):
        e = s.get(m.Evidence, int(evidence_id.lstrip("Ee")))
        if e is None:
            raise ApiError(404, "NOT_FOUND", f"evidence {evidence_id}")
        return {"id": f"E{e.id}", "kind": e.kind, "source": e.source, "source_ref": e.source_ref,
                "as_of": e.as_of.isoformat(), "payload": e.payload, "untrusted": e.kind in ("news", "filing")}

    # -- on-demand analysis --------------------------------------------------------------
    @app.post(f"{api}/analyze", dependencies=protected, status_code=202)
    async def start_analysis(body: AnalyzeIn, request: Request):
        bad = [h for h in body.horizons if h not in ("short", "medium", "long")]
        if bad or not body.symbol.strip():
            raise ApiError(422, "INVALID_ARGUMENT", "symbol required; horizons must be short/medium/long", bad)
        job = request.app.state.jobs.start_analysis(request.app.state.deps, body.symbol.strip(), body.horizons)
        return {"job_id": job.id, "events_url": f"{api}/jobs/{job.id}/events"}

    @app.get(f"{api}/jobs/{{job_id}}", dependencies=protected)
    def job(job_id: int, request: Request):
        j = request.app.state.jobs.get(job_id)
        if j is None:
            raise ApiError(404, "NOT_FOUND", f"job {job_id}")
        return j.view()

    @app.get(f"{api}/jobs/{{job_id}}/events", dependencies=protected)
    async def job_events(job_id: int, request: Request):
        j = request.app.state.jobs.get(job_id)
        if j is None:
            raise ApiError(404, "NOT_FOUND", f"job {job_id}")

        async def stream():
            sent = 0
            while True:
                while sent < len(j.events):
                    yield {"event": j.events[sent]["kind"], "data": json.dumps(j.events[sent])}
                    sent += 1
                if j.status != "running":
                    yield {"event": "result", "data": json.dumps(j.view(), default=str)}
                    return
                j.changed.clear()
                try:
                    await asyncio.wait_for(j.changed.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        return EventSourceResponse(stream())

    # -- portfolios ----------------------------------------------------------------------
    @app.get(f"{api}/portfolios", dependencies=protected)
    def portfolios(s=Depends(db)):
        out = []
        for p in s.scalars(select(m.Portfolio).where(m.Portfolio.archived.is_(False))):
            perf = performance(s, p.id)
            out.append({"id": p.id, "name": p.name, "type": p.type, "starting_cash": float(p.starting_cash),
                        "cash": float(p.cash), "benchmark": p.benchmark, "metrics": perf["metrics"],
                        "open_positions": s.scalar(select(func.count()).select_from(m.Position).where(
                            m.Position.portfolio_id == p.id, m.Position.status == "open"))})
        return {"items": out}

    @app.post(f"{api}/portfolios", dependencies=protected, status_code=201)
    def create_portfolio(body: PortfolioIn, request: Request):
        pid = request.app.state.sim.create_portfolio(body.name, body.starting_cash, body.type, body.benchmark, body.rules)
        return {"id": pid}

    @app.get(f"{api}/portfolios/{{pid}}", dependencies=protected)
    def portfolio(pid: int, s=Depends(db)):
        p = s.get(m.Portfolio, pid)
        if p is None:
            raise ApiError(404, "NOT_FOUND", f"portfolio {pid}")
        positions = []
        for pos in s.scalars(select(m.Position).where(m.Position.portfolio_id == pid, m.Position.status == "open")):
            inst = s.get(m.Instrument, pos.instrument_id)
            last = s.scalar(select(m.PriceDaily.close).where(m.PriceDaily.instrument_id == pos.instrument_id)
                            .order_by(m.PriceDaily.trade_date.desc()).limit(1))
            last = _f(last) or float(pos.avg_cost)
            positions.append({"symbol": inst.symbol, "horizon": pos.horizon, "qty": pos.qty, "avg_cost": float(pos.avg_cost),
                              "last": last, "pnl_pct": round((last / float(pos.avg_cost) - 1) * 100, 2),
                              "stop": _f(pos.stop), "target": _f(pos.target), "opened_on": pos.opened_on.isoformat(),
                              "expires_on": pos.expires_on.isoformat() if pos.expires_on else None, "reco_id": pos.reco_id})
        return {**performance(s, pid), "rules": p.rules, "positions": positions}

    @app.get(f"{api}/portfolios/{{pid}}/performance", dependencies=protected)
    def portfolio_performance(pid: int, from_: Optional[date] = Query(default=None, alias="from"),
                              to: Optional[date] = None, s=Depends(db)):
        if s.get(m.Portfolio, pid) is None:
            raise ApiError(404, "NOT_FOUND", f"portfolio {pid}")
        return performance(s, pid, from_, to)

    @app.get(f"{api}/portfolios/{{pid}}/trades", dependencies=protected)
    def trades(pid: int, s=Depends(db)):
        rows = s.execute(select(m.PaperTrade, m.Instrument.symbol).join(m.Instrument).where(m.PaperTrade.portfolio_id == pid)
                         .order_by(m.PaperTrade.fill_date.desc(), m.PaperTrade.id.desc()).limit(500)).all()
        return {"items": [{"id": t.id, "symbol": sym, "side": t.side, "qty": t.qty, "price": float(t.fill_price),
                           "date": t.fill_date.isoformat(), "reason": t.reason, "costs": _f(t.total_cost),
                           "reco_id": t.reco_id} for t, sym in rows]}

    @app.get(f"{api}/portfolios/{{pid}}/orders", dependencies=protected)
    def orders(pid: int, s=Depends(db)):
        rows = s.execute(select(m.PaperOrder, m.Instrument.symbol).join(m.Instrument).where(m.PaperOrder.portfolio_id == pid)
                         .order_by(m.PaperOrder.id.desc()).limit(200)).all()
        return {"items": [{"id": o.id, "symbol": sym, "status": o.status, "qty": o.qty, "amount": _f(o.amount),
                           "stop": _f(o.stop), "target": _f(o.target), "placed_at": o.placed_at.isoformat(),
                           "reject_reason": o.reject_reason, "reco_id": o.reco_id} for o, sym in rows]}

    @app.post(f"{api}/portfolios/{{pid}}/orders", dependencies=protected, status_code=201)
    def place_order(pid: int, body: OrderIn, request: Request,
                    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")):
        if not idempotency_key:
            raise ApiError(400, "INVALID_ARGUMENT", "Idempotency-Key header is required")
        symbol, stop, target, horizon = body.symbol, body.stop, body.target, body.horizon
        if body.reco_id is not None:   # prefill from the card; caller may override stop/target
            with request.app.state.f() as s:
                r = s.get(m.Recommendation, body.reco_id)
                if r is None:
                    raise ApiError(404, "NOT_FOUND", f"recommendation {body.reco_id}")
                symbol = symbol or s.get(m.Instrument, r.instrument_id).symbol
                stop = stop if stop is not None else _f(r.stop)
                target = target if target is not None else _f(r.target)
                horizon = horizon or r.horizon
        if not symbol:
            raise ApiError(422, "INVALID_ARGUMENT", "symbol or reco_id required")
        try:
            return request.app.state.sim.place_order(pid, symbol, amount=body.amount, qty=body.qty, stop=stop,
                                                     target=target, horizon=horizon, reco_id=body.reco_id,
                                                     idempotency_key=idempotency_key)
        except OrderRejected as e:
            raise ApiError(422, "ORDER_REJECTED", str(e))

    # -- HITL ------------------------------------------------------------------------------
    @app.get(f"{api}/hitl/tasks", dependencies=protected)
    def tasks(request: Request, status: str = "pending", limit: int = Query(100, le=500)):
        return {"items": request.app.state.hitl.list_tasks(status, limit)}

    @app.get(f"{api}/hitl/tasks/{{task_id}}", dependencies=protected)
    def task(task_id: int, request: Request):
        try:
            return request.app.state.hitl.get(task_id)
        except DecisionError as e:
            raise ApiError(404, "NOT_FOUND", str(e))

    @app.post(f"{api}/hitl/tasks/{{task_id}}/decision", dependencies=protected)
    def decide(task_id: int, body: DecisionIn, request: Request):
        try:
            res = request.app.state.hitl.decide(task_id, body.action, note=body.note, edits=body.edits)
        except DecisionError as e:
            raise ApiError(409 if "already" in str(e) else 422, "DECISION_REJECTED", str(e))
        return res.__dict__

    # -- scorecard, runs, config ------------------------------------------------------------
    @app.get(f"{api}/scorecard", dependencies=protected)
    def get_scorecard(s=Depends(db)):
        return scorecard(s)

    @app.get(f"{api}/runs", dependencies=protected)
    def runs(limit: int = Query(50, le=200), run_type: Optional[str] = None, s=Depends(db)):
        q = select(m.Run)
        if run_type:
            q = q.where(m.Run.run_type == run_type)
        rows = s.scalars(q.order_by(m.Run.id.desc()).limit(limit)).all()
        return {"items": [{"id": r.id, "run_type": r.run_type, "trigger": r.trigger, "status": r.status,
                           "decision_time": r.decision_time.isoformat(), "spent_usd": _f(r.spent_usd),
                           "started_at": r.started_at.isoformat(),
                           "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in rows]}

    @app.get(f"{api}/runs/{{run_id}}", dependencies=protected)
    def run_detail(run_id: int, s=Depends(db)):
        r = s.get(m.Run, run_id)
        if r is None:
            raise ApiError(404, "NOT_FOUND", f"run {run_id}")
        tcs = s.scalars(select(m.ToolCall).where(m.ToolCall.run_id == run_id).order_by(m.ToolCall.id)).all()
        lcs = s.scalars(select(m.LlmCall).where(m.LlmCall.run_id == run_id).order_by(m.LlmCall.id)).all()
        return {"id": r.id, "run_type": r.run_type, "trigger": r.trigger, "status": r.status, "error": r.error,
                "decision_time": r.decision_time.isoformat(), "spent_usd": _f(r.spent_usd),
                "config_version": r.config_version, "prompt_bundle_version": r.prompt_bundle_version,
                "tool_calls": [{"tool": t.tool, "agent": t.agent, "ok": t.ok, "error_code": t.error_code,
                                "evidence": f"E{t.result_ref}" if t.result_ref else None, "latency_ms": t.latency_ms}
                               for t in tcs],
                "llm_calls": [{"call_id": c.call_id, "agent": c.agent, "model": c.model_id, "tokens_in": c.tokens_in,
                               "tokens_out": c.tokens_out, "cost_usd": _f(c.cost_usd), "latency_ms": c.latency_ms,
                               "validation": c.validation_status, "retries": c.retries} for c in lcs],
                "recommendations": [x.id for x in s.scalars(select(m.Recommendation).where(m.Recommendation.run_id == run_id))]}

    @app.get(f"{api}/config", dependencies=protected)
    def config(request: Request):
        return request.app.state.deps.cfg.model_dump(mode="json")

    @app.post(f"{api}/config/autonomy", dependencies=protected, status_code=202)
    def request_autonomy(body: AutonomyIn, request: Request, s=Depends(db)):
        """§8.2: changing an autonomy level is itself a config change that needs approval (approve_config task)."""
        from orchestrator.wiring import load_config

        current = load_config(request.app.state.f)
        old = int(current.autonomy.get(body.action, 0))
        if old == body.level:
            raise ApiError(409, "NO_CHANGE", f"{body.action} is already L{old}")
        proposed = current.model_copy(deep=True)
        from orchestrator.config import Autonomy

        proposed.autonomy[body.action] = Autonomy(body.level)
        row = m.ConfigVersion(config=proposed.model_dump(mode="json"), created_by="owner:request", approved=False)
        s.add(row)
        s.commit()
        tid = request.app.state.deps.store.create_hitl_task(
            "approve_config", "config_version", row.version,
            payload={"diff": {body.action: {"from": old, "to": body.level}}, "note": body.note},
            ai_recommendation="review", reason=f"{body.action}: L{old} -> L{body.level}", priority=2,
            default_on_expiry="reject")
        return {"config_version": row.version, "task_id": tid, "status": "pending approval"}

    def save_config(request: Request, s, change, created_by: str):
        """Non-sensitive switches: start from the latest approved version (not the API's in-memory copy, which can
        be older than a change approved since startup), apply, store as approved, and use it from now on."""
        from orchestrator.wiring import load_config

        cfg = load_config(request.app.state.f)
        change(cfg)
        row = m.ConfigVersion(config=cfg.model_dump(mode="json"), created_by=created_by, approved=True)
        s.add(row)
        s.commit()
        cfg.version = row.version
        request.app.state.deps.cfg = cfg
        return cfg

    @app.post(f"{api}/admin/kill-switch", dependencies=protected)
    def kill_switch(body: KillSwitchIn, request: Request, s=Depends(db)):
        """F8: one toggle pauses publishing (everything goes to review). Recorded as an approved config version."""
        cfg = save_config(request, s, lambda c: setattr(c, "kill_switch", body.enabled), "kill_switch")
        return {"kill_switch": cfg.kill_switch}

    @app.post(f"{api}/config/alerts", dependencies=protected)
    def set_alerts(body: AlertsIn, request: Request, s=Depends(db)):
        """Optional intraday stop-proximity alerts. Alerts only, so no approval step (unlike autonomy)."""
        def change(c):
            c.intraday_alerts, c.stop_proximity_pct = body.enabled, body.stop_proximity_pct
        cfg = save_config(request, s, change, "owner:alerts")
        return {"intraday_alerts": cfg.intraday_alerts, "stop_proximity_pct": cfg.stop_proximity_pct}

    @app.get(f"{api}/alerts", dependencies=protected)
    def alerts(days: int = Query(7, ge=1, le=90), s=Depends(db)):
        since = datetime.now(timezone.utc).date() - timedelta(days=days)
        rows = s.scalars(select(m.AlertLog).where(m.AlertLog.alert_date >= since).order_by(m.AlertLog.id.desc()).limit(500))
        return {"items": [{"id": x.id, "date": x.alert_date.isoformat(), "kind": x.kind, "symbol": x.symbol, "subject": x.subject_key,
                           "price": float(x.price) if x.price is not None else None, "level": float(x.level) if x.level is not None else None,
                           "label": (x.payload or {}).get("label"), "sent_at": x.sent_at.isoformat()} for x in rows]}

    # -- Phase 3: market & events (S5), discovery (A3) ----------------------------------------
    @app.post(f"{api}/discover", dependencies=protected, status_code=202)
    async def start_discover(body: DiscoverIn, request: Request):
        bad = [h for h in body.horizons if h not in ("short", "medium", "long")]
        if bad or not body.horizons:
            raise ApiError(422, "INVALID_ARGUMENT", "horizons must be short/medium/long", bad)
        constraints = {"sector": body.sector} if body.sector else {}
        job = request.app.state.jobs.start_discover(request.app.state.deps, body.horizons, constraints, body.max_ideas)
        return {"job_id": job.id, "events_url": f"{api}/jobs/{job.id}/events"}

    @app.post(f"{api}/chat", dependencies=protected, status_code=202)
    async def start_chat(body: ChatIn, request: Request):
        """C3 grounded chat: answers come from tools and stored evidence, cited as [E123]."""
        job = request.app.state.jobs.start_chat(request.app.state.deps, body.message.strip(), body.history)
        return {"job_id": job.id, "events_url": f"{api}/jobs/{job.id}/events"}

    @app.get(f"{api}/market/macro", dependencies=protected)
    def macro(days: int = Query(60, le=400), s=Depends(db)):
        out = {}
        for sid in ("BRENT", "USDINR", "US10Y", "GOLD", "DXY"):
            rows = s.scalars(select(m.MacroSeries).where(m.MacroSeries.series_id == sid)
                             .order_by(m.MacroSeries.obs_date.desc()).limit(days)).all()[::-1]
            vals = [float(r.value) for r in rows]
            out[sid] = {"points": [{"date": r.obs_date.isoformat(), "value": float(r.value)} for r in rows],
                        "last": vals[-1] if vals else None,
                        "chg_1d": (vals[-1] - vals[-2] if sid == "US10Y" else (vals[-1] / vals[-2] - 1) * 100) if len(vals) >= 2 else None,
                        "chg_5d": (vals[-1] - vals[-6] if sid == "US10Y" else (vals[-1] / vals[-6] - 1) * 100) if len(vals) >= 6 else None,
                        "unit": "pp" if sid == "US10Y" else "%"}
        flows = s.scalars(select(m.FlowsDaily).order_by(m.FlowsDaily.trade_date.desc()).limit(20)).all()[::-1]
        return {"series": out, "flows": [{"date": x.trade_date.isoformat(), "fii_net_cr": _f(x.fii_net),
                                          "dii_net_cr": _f(x.dii_net)} for x in flows]}

    @app.get(f"{api}/market/sectors", dependencies=protected)
    def sectors(request: Request, s=Depends(db)):
        """Sector index returns plus today's net event overlay (diverging: + tailwind, − headwind)."""
        from quant.data import load_index
        from quant.engine import SECTOR_INDEX, event_overlay

        last = s.scalar(select(func.max(m.IndexPriceDaily.trade_date)))
        if last is None:
            return {"as_of": None, "sectors": []}
        sector_events, _ = event_overlay(s, last)
        rows = []
        for sector, code in sorted(SECTOR_INDEX.items()):
            ser = load_index(s, code, last, 130)
            r1 = (ser.iloc[-1] / ser.iloc[-22] - 1) * 100 if len(ser) >= 22 else None
            r3 = (ser.iloc[-1] / ser.iloc[-64] - 1) * 100 if len(ser) >= 64 else None
            r5d = (ser.iloc[-1] / ser.iloc[-6] - 1) * 100 if len(ser) >= 6 else None
            rows.append({"sector": sector, "index": code, "ret_5d_pct": _r(r5d), "ret_1m_pct": _r(r1), "ret_3m_pct": _r(r3),
                         "event_overlay": sector_events.get(sector, 0.0)})
        for sector, v in sector_events.items():          # event-hit sectors without an NSE index
            if sector not in SECTOR_INDEX:
                rows.append({"sector": sector, "index": None, "ret_5d_pct": None, "ret_1m_pct": None, "ret_3m_pct": None,
                             "event_overlay": v})
        return {"as_of": last.isoformat(), "sectors": rows}

    @app.get(f"{api}/market/calendar", dependencies=protected)
    def calendar(days: int = Query(30, le=90), scope: str = "watch", start: Optional[date] = None, s=Depends(db)):
        """Results dates, ex-dates and exchange holidays. scope=watch limits results to open picks + universe."""
        from ingestion.reference import universe_members

        today = start or datetime.now(IST).date()
        end = today + timedelta(days=days)
        open_ids = set(s.scalars(select(m.Recommendation.instrument_id).where(m.Recommendation.status.in_(("published", "active")))))
        members = set(universe_members(s, get_settings().universe, today))
        items = []
        for ca, sym in s.execute(select(m.CorporateAction, m.Instrument.symbol).join(m.Instrument).where(
                m.CorporateAction.ex_date >= today, m.CorporateAction.ex_date <= end).order_by(m.CorporateAction.ex_date)):
            if scope == "watch" and ca.instrument_id not in open_ids and ca.instrument_id not in members:
                continue
            items.append({"date": ca.ex_date.isoformat(), "symbol": sym, "kind": ca.action_type,
                          "detail": (ca.details or {}).get("subject"), "open_pick": ca.instrument_id in open_ids})
        for h in s.scalars(select(m.TradingCalendar).where(m.TradingCalendar.cal_date >= today, m.TradingCalendar.cal_date <= end,
                                                           m.TradingCalendar.is_trading_day.is_(False), m.TradingCalendar.note != "weekend")):
            items.append({"date": h.cal_date.isoformat(), "symbol": None, "kind": "holiday", "detail": h.note, "open_pick": False})
        items.sort(key=lambda x: (x["date"], not x["open_pick"], x["symbol"] or ""))
        return {"from": today.isoformat(), "to": end.isoformat(), "items": items[:500]}

    @app.get(f"{api}/market/sensitivity", dependencies=protected)
    def sensitivity(s=Depends(db)):
        rows = s.execute(select(m.SectorSensitivity, m.MacroFactor.code, m.MacroFactor.name, m.Sector.name)
                         .join(m.MacroFactor, m.MacroFactor.id == m.SectorSensitivity.factor_id)
                         .join(m.Sector, m.Sector.id == m.SectorSensitivity.sector_id)
                         .where(m.SectorSensitivity.status.in_(("approved", "proposed")))
                         .order_by(m.MacroFactor.code, m.Sector.name)).all()
        return {"rows": [{"id": r.id, "factor": code, "factor_name": fname, "sector": sector, "direction": r.direction,
                          "magnitude": float(r.magnitude), "rationale": r.rationale, "status": r.status, "version": r.version,
                          "approved_by": r.approved_by, "needs_review": (r.approved_by or "").startswith("seed")}
                         for r, code, fname, sector in rows]}

    @app.get(f"{api}/premarket", dependencies=protected)
    def premarket(s=Depends(db)):
        row = s.scalar(select(m.AgentOutput).where(m.AgentOutput.agent == "premarket").order_by(m.AgentOutput.id.desc()).limit(1))
        if row is None:
            raise ApiError(404, "NOT_FOUND", "no pre-market brief yet")
        return row.output

    # -- Phase 5: what-if / stress (B6, D10), comparison (A7), export (D11) ---------------------
    @app.get(f"{api}/stress/presets", dependencies=protected)
    def stress_presets(s=Depends(db)):
        from simulator.stress import FACTORS, PRESETS, StressEngine

        e = StressEngine(s)
        return {"as_of": e.as_of.isoformat() if e.as_of else None, "presets": [{"id": k, **v} for k, v in PRESETS.items()],
                "factors": [{"code": k, "label": lab, "unit": unit} for k, (lab, unit) in FACTORS.items()],
                "latest": e.latest_levels()}

    @app.post(f"{api}/stress", dependencies=protected)
    def stress(body: StressIn, s=Depends(db)):
        from dataclasses import asdict

        from simulator.stress import PRESETS, Shock, StressEngine, parse_scenario

        e = StressEngine(s)
        if e.as_of is None:
            raise ApiError(409, "NO_DATA", "no price data yet")
        notes: list[str] = []
        try:
            if body.preset:
                if body.preset not in PRESETS:
                    raise ApiError(422, "INVALID_ARGUMENT", f"unknown preset {body.preset}", list(PRESETS))
                name, shocks = PRESETS[body.preset]["name"], [Shock(**x) for x in PRESETS[body.preset]["shocks"]]
            elif body.text:
                shocks, notes = parse_scenario(body.text, e.latest_levels())
                name = body.text.strip()
                if not shocks:
                    raise ApiError(422, "UNPARSED", "Couldn't find a shock in that. Try 'crude hits $110', 'rupee falls 3%', "
                                   "'US yields +50bp' or 'Nifty falls 10%'.", notes)
            else:
                shocks, name = [Shock(x.factor, x.change) for x in body.shocks], "Custom scenario"
            return asdict(e.run(shocks, body.portfolio_ids or None, name=name, notes=notes))
        except ValueError as ex:
            raise ApiError(422, "INVALID_ARGUMENT", str(ex))

    @app.post(f"{api}/stress/replay", dependencies=protected)
    def stress_replay(body: ReplayIn, s=Depends(db)):
        from simulator.stress import StressEngine

        if (body.start is None) != (body.end is None) or (body.start and body.start >= body.end):
            raise ApiError(422, "INVALID_ARGUMENT", "give both start and end (start before end), or neither")
        try:
            return StressEngine(s).replay(body.days, body.start, body.end, body.portfolio_ids or None)
        except ValueError as ex:
            raise ApiError(422, "INVALID_ARGUMENT", str(ex))

    @app.get(f"{api}/compare", dependencies=protected)
    def compare_view(request: Request, symbols: str = Query(..., description="2-5 comma-separated symbols"),
                     days: int = Query(250, ge=20, le=400), s=Depends(db)):
        from quant.compare import compare
        from quant.engine import QuantEngine, UnknownSymbol

        if getattr(request.app.state, "quant", None) is None:
            request.app.state.quant = QuantEngine(request.app.state.f, get_settings())
        as_of = s.scalar(select(func.max(m.PriceDaily.trade_date)))
        try:
            return compare(s, request.app.state.quant, symbols.split(","), as_of, days)
        except UnknownSymbol as ex:
            raise ApiError(404, "NOT_FOUND", f"unknown symbol: {ex}")
        except ValueError as ex:
            raise ApiError(422, "INVALID_ARGUMENT", str(ex))

    def csv_response(text: str, filename: str):
        from fastapi.responses import Response

        return Response(text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get(f"{api}/export/portfolios/{{pid}}/{{kind}}.csv", dependencies=protected)
    def export_portfolio(pid: int, kind: str, s=Depends(db)):
        from evaluation.export import nav_csv, positions_csv, trades_csv

        fn = {"trades": trades_csv, "nav": nav_csv, "positions": positions_csv}.get(kind)
        if fn is None:
            raise ApiError(404, "NOT_FOUND", "export kind must be trades, nav or positions")
        p = s.get(m.Portfolio, pid)
        if p is None:
            raise ApiError(404, "NOT_FOUND", f"portfolio {pid}")
        slug = "".join(ch if ch.isalnum() else "-" for ch in p.name.lower()).strip("-") or f"portfolio-{pid}"
        return csv_response(fn(s, pid), f"{slug}-{kind}.csv")

    @app.get(f"{api}/export/recommendations.csv", dependencies=protected)
    def export_recos(horizon: Optional[str] = None, status: Optional[str] = None, s=Depends(db)):
        from evaluation.export import recommendations_csv

        return csv_response(recommendations_csv(s, horizon, status), "recommendations.csv")

    return app


def _last_backup() -> Optional[dict]:
    try:
        from ops.backup import latest_entry

        e = latest_entry(get_settings().backup_dir)
        return None if e is None else {"created_at": e["created_at"], "encrypted": e["encrypted"], "size": e["size"]}
    except Exception:
        return None


def _r(x) -> Optional[float]:
    return None if x is None else round(float(x), 2)


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("api.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
