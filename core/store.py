"""SQL implementation of the orchestrator's Store port (orchestrator/ports.py).

Works on SQLite now and Postgres later. Runs, LLM calls, tool calls and
recommendations are append-only; state changes go through status columns.
"""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.ports import EvidenceRow

from . import models as m
from .db import session_factory


def _json_safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _dec(x: Any) -> Optional[Decimal]:
    return None if x is None else Decimal(str(x))


def _f(x: Any) -> Optional[float]:
    return None if x is None else float(x)


from orchestrator.sla import default_due  # noqa: E402  (re-exported)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class SqlStore:
    def __init__(self, factory: sessionmaker[Session] | None = None):
        self._factory = factory or session_factory()
        bind = self._factory.kw.get("bind")
        # SQLite allows one writer and our in-memory test DB shares one connection: serialise access.
        self._lock = threading.RLock() if bind is not None and bind.dialect.name == "sqlite" else nullcontext()

    @contextmanager
    def _tx(self) -> Iterator[Session]:
        with self._lock, self._factory() as s, s.begin():
            yield s

    @contextmanager
    def _read(self) -> Iterator[Session]:
        with self._lock, self._factory() as s:
            yield s

    # -- helpers ----------------------------------------------------------------
    @staticmethod
    def instrument_id(s: Session, symbol: str, exchange: str = "NSE") -> int:
        iid = s.scalar(select(m.Instrument.id).where(m.Instrument.exchange == exchange, m.Instrument.symbol == symbol))
        if iid is None:
            inst = m.Instrument(symbol=symbol, exchange=exchange, name=symbol)
            s.add(inst)
            s.flush()
            iid = inst.id
        return iid

    @staticmethod
    def sector_id(s: Session, name: Optional[str]) -> Optional[int]:
        if not name:
            return None
        sid = s.scalar(select(m.Sector.id).where(m.Sector.name == name))
        if sid is None:
            sec = m.Sector(name=name)
            s.add(sec)
            s.flush()
            sid = sec.id
        return sid

    # -- runs -------------------------------------------------------------------
    def create_run(self, run_type, decision_time, trigger, config_version, strategy_version, prompt_bundle_version):
        with self._tx() as s:
            run = m.Run(run_type=run_type, trigger=trigger, decision_time=decision_time, status="running",
                        config_version=config_version, strategy_version=strategy_version,
                        prompt_bundle_version=prompt_bundle_version)
            s.add(run)
            s.flush()
            return run.id

    def finish_run(self, run_id, status, spent_usd, error=None):
        with self._tx() as s:
            run = s.get(m.Run, run_id)
            run.status = status
            run.spent_usd = _dec(round(spent_usd, 4))
            run.finished_at = datetime.now(timezone.utc)
            run.error = _json_safe(error) if error else None

    def find_run(self, run_type, decision_date):
        with self._read() as s:
            runs = s.scalars(select(m.Run).where(m.Run.run_type == run_type).order_by(m.Run.id.desc())).all()
            return next((r.id for r in runs if r.decision_time.date().isoformat() == decision_date), None)

    # -- evidence ----------------------------------------------------------------
    def add_evidence(self, kind: str, source: str, as_of: datetime, payload: Any, run_id: Optional[int],
                     source_ref: Optional[str] = None) -> int:
        payload = _json_safe(payload if isinstance(payload, dict) else {"value": payload})
        h = payload_hash(payload)
        with self._tx() as s:
            # Same fact (payload, kind, source, as-of) -> same evidence row, across runs. Stable IDs make
            # identical prompts identical, which is what lets the LLM response cache hit (§4.8).
            # Kind and source must match too: two tools can return identical JSON (e.g. empty lists).
            existing = s.scalar(select(m.Evidence.id).where(m.Evidence.payload_hash == h, m.Evidence.as_of == as_of,
                                                            m.Evidence.kind == kind, m.Evidence.source == source)
                                .order_by(m.Evidence.id).limit(1))
            if existing:
                return existing
            row = m.Evidence(kind=kind, source=source, source_ref=source_ref, as_of=as_of, payload=payload,
                             payload_hash=h, run_id=run_id)
            s.add(row)
            s.flush()
            return row.id

    def get_evidence(self, evidence_id):
        with self._read() as s:
            r = s.get(m.Evidence, evidence_id)
            if r is None:
                return None
            data = r.payload.get("value") if set(r.payload) == {"value"} else r.payload
            return EvidenceRow(r.id, r.kind, r.source, r.as_of, data, r.run_id)

    # -- audit -------------------------------------------------------------------
    def record_llm_call(self, row):
        with self._tx() as s:
            s.add(m.LlmCall(**{**row, "request": _json_safe(row.get("request")),
                               "response": _json_safe(row.get("response")), "cost_usd": _dec(row.get("cost_usd"))}))

    def cached_llm_response(self, call_id: str, input_hash: str, max_age_hours: float) -> Optional[dict]:
        since = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        with self._read() as s:
            row = s.scalar(select(m.LlmCall).where(
                m.LlmCall.call_id == call_id, m.LlmCall.input_hash == input_hash, m.LlmCall.created_at >= since,
                m.LlmCall.validation_status.in_(("passed", "repaired", "cached"))).order_by(m.LlmCall.id.desc()).limit(1))
            return row.response if row is not None else None

    def record_tool_call(self, row: dict) -> None:
        with self._tx() as s:
            s.add(m.ToolCall(**{**row, "args": _json_safe(row.get("args"))}))

    def record_security_event(self, row):
        with self._tx() as s:
            s.add(m.SecurityEvent(run_id=row.get("run_id"), agent=row.get("agent"), source=row.get("source"),
                                  news_id=row.get("news_id"), flag=row.get("flag"),
                                  excerpt_hash=row.get("excerpt_hash"), action_taken=row.get("action_taken")))

    def save_agent_output(self, run_id, agent, subject_type, subject_id, output, validation_status, confidence):
        with self._tx() as s:
            s.add(m.AgentOutput(run_id=run_id, agent=agent, subject_type=subject_type, subject_id=subject_id,
                                output=_json_safe(output), validation_status=validation_status,
                                confidence=_dec(confidence)))

    def spent_since(self, since: datetime) -> float:
        with self._read() as s:
            return float(s.scalar(select(func.coalesce(func.sum(m.LlmCall.cost_usd), 0))
                                  .where(m.LlmCall.created_at >= since)) or 0)

    # -- market context ------------------------------------------------------------
    def save_regime(self, as_of, row):
        inputs = row.get("inputs", {}) or {}
        with self._tx() as s:
            snap = s.get(m.RegimeSnapshot, date.fromisoformat(as_of)) or m.RegimeSnapshot(as_of=date.fromisoformat(as_of))
            snap.regime = row["regime"]
            snap.score = _dec(row.get("score"))
            snap.india_vix = _dec(inputs.get("india_vix"))
            snap.narrative = (row.get("assessment") or {}).get("narrative")
            snap.details = _json_safe({"inputs": inputs, "assessment": row.get("assessment")})
            s.merge(snap)

    def latest_regime(self):
        with self._read() as s:
            snap = s.scalar(select(m.RegimeSnapshot).order_by(m.RegimeSnapshot.as_of.desc()).limit(1))
            if snap is None:
                return None
            d = snap.details or {}
            return {"as_of": snap.as_of.isoformat(), "regime": snap.regime, "score": _f(snap.score),
                    "inputs": d.get("inputs", {}), "assessment": d.get("assessment") or {}}

    _EVENT_COLS = {"event_key", "event_type", "title", "summary", "geographies", "severity", "confidence",
                   "status", "source_news_ids", "extracted_by_run"}

    def save_event(self, event):
        with self._tx() as s:
            first_seen = event.get("first_seen")
            row = m.Event(**{k: v for k, v in event.items() if k in self._EVENT_COLS and k != "confidence"},
                          confidence=_dec(event.get("confidence")),
                          first_seen=datetime.fromisoformat(first_seen) if isinstance(first_seen, str) else first_seen,
                          details=_json_safe({k: v for k, v in event.items()
                                              if k not in self._EVENT_COLS | {"first_seen", "id"}}))
            s.add(row)
            s.flush()
            return row.id

    def event_exists(self, event_key):
        with self._read() as s:
            return s.scalar(select(m.Event.id).where(m.Event.event_key == event_key)) is not None

    def save_event_impacts(self, event_id, impacts, run_id):
        with self._tx() as s:
            for imp in impacts.get("impacts", []):
                s.add(m.EventImpact(
                    event_id=event_id, sector_id=self.sector_id(s, imp["sector"]), direction=imp["direction"],
                    magnitude=_dec(imp["magnitude"]), horizon=imp.get("horizon"), channel=imp.get("channel"),
                    rationale=imp.get("rationale"), evidence_ids=imp.get("evidence_ids"), run_id=run_id,
                    details={"basis": imp.get("basis"), "map_row_ids": imp.get("map_row_ids", []),
                             "confidence": imp.get("confidence"), "decay_days": impacts.get("decay_days")}))
            for st in impacts.get("stock_specific", []):
                s.add(m.EventImpact(event_id=event_id, instrument_id=self.instrument_id(s, st["symbol"]),
                                    direction=st["direction"], magnitude=Decimal("0"), rationale=st.get("reason"),
                                    evidence_ids=st.get("evidence_ids"), run_id=run_id, details={"basis": "stock"}))

    def active_events(self, since):
        with self._read() as s:
            events = s.scalars(select(m.Event).where(m.Event.status.in_(("active", "unverified")))).all()
            out = []
            for e in events:
                if e.first_seen and e.first_seen < since:
                    continue
                rows = s.execute(select(m.EventImpact, m.Sector.name).outerjoin(m.Sector, m.EventImpact.sector_id == m.Sector.id)
                                 .where(m.EventImpact.event_id == e.id, m.EventImpact.sector_id.is_not(None))).all()
                impacts = [{"sector": name, "direction": i.direction, "magnitude": float(i.magnitude),
                            "horizon": i.horizon, "channel": i.channel, "rationale": i.rationale,
                            "evidence_ids": i.evidence_ids or [], **(i.details or {})} for i, name in rows]
                out.append({"id": e.id, "event_key": e.event_key, "event_type": e.event_type, "title": e.title,
                            "summary": e.summary, "geographies": e.geographies, "severity": e.severity,
                            "confidence": _f(e.confidence), "status": e.status,
                            "source_news_ids": e.source_news_ids or [], **(e.details or {}), "impacts": impacts})
            return out

    # -- recommendations ---------------------------------------------------------
    _RECO_META = ("run_type", "violations", "gaps", "critique", "evidence_ids", "sector", "symbol", "contributions")

    def save_recommendation(self, reco):
        with self._tx() as s:
            iid = self.instrument_id(s, reco["symbol"])
            if reco.get("sector"):
                inst = s.get(m.Instrument, iid)
                if inst.sector_id is None:
                    inst.sector_id = self.sector_id(s, reco["sector"])
            row = m.Recommendation(
                run_id=reco["run_id"], instrument_id=iid, horizon=reco["horizon"], verdict=reco["verdict"],
                action=reco.get("action"), quant_score=_dec(reco["quant_score"]),
                agent_adjustment=_dec(reco.get("agent_adjustment", 0)), final_score=_dec(reco["final_score"]),
                confidence=_dec(reco.get("confidence")), entry_low=_dec(reco.get("entry_low")),
                entry_high=_dec(reco.get("entry_high")), stop=_dec(reco.get("stop")), target=_dec(reco.get("target")),
                risk_reward=_dec(reco.get("risk_reward")), thesis=reco.get("thesis") or "",
                bull_case=reco.get("bull_case"), bear_case=reco.get("bear_case"),
                key_risks=_json_safe(reco.get("key_risks")), invalidation=_json_safe(reco.get("invalidation")),
                regime_at_issue=reco.get("regime_at_issue"), card=_json_safe(reco.get("card") or {}),
                status=reco.get("status", "draft"), strategy_version=reco.get("strategy_version"),
                prompt_bundle_version=reco.get("prompt_bundle_version"),
                meta=_json_safe({k: reco.get(k) for k in self._RECO_META}))
            s.add(row)
            s.flush()
            for ref in reco.get("evidence_ids", []):
                eid = int(str(ref).lstrip("E"))
                if s.get(m.Evidence, eid) is not None:
                    s.merge(m.RecoEvidence(reco_id=row.id, evidence_id=eid, role="supports"))
            return row.id

    def set_reco_status(self, reco_id, status, **fields):
        with self._tx() as s:
            r = s.get(m.Recommendation, reco_id)
            r.status = status
            for k, v in fields.items():
                if k in ("published_at", "closed_at") and isinstance(v, str):
                    v = datetime.fromisoformat(v)
                setattr(r, k, v)

    def _reco_dict(self, s: Session, r: m.Recommendation) -> dict:
        inst = s.get(m.Instrument, r.instrument_id)
        sector = s.get(m.Sector, inst.sector_id).name if inst.sector_id else None
        return {"id": r.id, "symbol": inst.symbol, "sector": sector, "horizon": r.horizon, "verdict": r.verdict,
                "status": r.status, "final_score": _f(r.final_score), "confidence": _f(r.confidence),
                "entry_low": _f(r.entry_low), "entry_high": _f(r.entry_high), "stop": _f(r.stop),
                "target": _f(r.target), "card": r.card, "run_type": (r.meta or {}).get("run_type"),
                "published_at": r.published_at.isoformat() if r.published_at else None}

    def recent_reco(self, symbol, horizon, since):
        with self._read() as s:
            q = (select(m.Recommendation).join(m.Instrument, m.Recommendation.instrument_id == m.Instrument.id)
                 .where(m.Instrument.symbol == symbol, m.Recommendation.horizon == horizon,
                        m.Recommendation.status.in_(("published", "active")), m.Recommendation.published_at >= since)
                 .order_by(m.Recommendation.published_at.desc()).limit(1))
            r = s.scalar(q)
            return self._reco_dict(s, r) if r else None

    def open_recos(self, sectors=None):
        with self._read() as s:
            rows = s.scalars(select(m.Recommendation).where(m.Recommendation.status.in_(("published", "active")))).all()
            out = [self._reco_dict(s, r) for r in rows]
            return [r for r in out if sectors is None or r["sector"] in sectors]

    # -- HITL ----------------------------------------------------------------------
    def create_hitl_task(self, task_type, subject_type, subject_id, payload, ai_recommendation, reason,
                         priority=3, default_on_expiry="reject", due_at: Optional[datetime] = None):
        with self._tx() as s:
            t = m.HitlTask(task_type=task_type, subject_type=subject_type, subject_id=subject_id,
                           payload=_json_safe(payload), ai_recommendation=ai_recommendation, reason=reason,
                           priority=priority, default_on_expiry=default_on_expiry,
                           due_at=due_at or default_due(task_type, datetime.now(timezone.utc)))
            s.add(t)
            s.flush()
            return t.id

    # -- transmission map (LLM-13 proposals) -------------------------------------------
    def map_vocabulary(self) -> tuple[list[str], list[str]]:
        with self._read() as s:
            return (sorted(s.scalars(select(m.MacroFactor.code))), sorted(s.scalars(select(m.Sector.name))))

    def approved_map_row(self, factor: str, sector: str) -> Optional[int]:
        with self._read() as s:
            return s.scalar(select(m.SectorSensitivity.id)
                            .join(m.MacroFactor, m.MacroFactor.id == m.SectorSensitivity.factor_id)
                            .join(m.Sector, m.Sector.id == m.SectorSensitivity.sector_id)
                            .where(m.MacroFactor.code == factor, m.Sector.name == sector,
                                   m.SectorSensitivity.status == "approved"))

    def save_map_proposal(self, p: dict) -> int:
        with self._tx() as s:
            fid = s.scalar(select(m.MacroFactor.id).where(m.MacroFactor.code == p["factor"]))
            version = (s.scalar(select(func.max(m.SectorSensitivity.version))) or 0) + 1
            row = m.SectorSensitivity(factor_id=fid, sector_id=self.sector_id(s, p["sector"]), direction=p["direction"],
                                      magnitude=_dec(p["magnitude"]), rationale=f"[{p['action']}] {p['rationale']}",
                                      version=version, status="proposed", proposed_by="LLM-13")
            s.add(row)
            s.flush()
            return row.id
