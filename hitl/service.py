"""Review-inbox decisions and their effects (§8.3, §8.4).

Every decision stores who, when, the AI recommendation at the time, edits and the note.
On expiry the safe default applies: picks are not published, maps and configs do not change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import models as m

ACTIONS: dict[str, set[str]] = {
    "approve_reco": {"approve", "reject", "edit"},
    "resolve_conflict": {"approve", "neutral", "reject"},
    "approve_map_change": {"approve", "reject", "edit"},
    "data_anomaly": {"exclude", "accept"},
    "review_thesis_break": {"close", "hold", "adjust_stop", "dismiss"},
    "approve_config": {"approve", "reject"},
    "high_severity_event": {"confirm", "downgrade", "dismiss", "trigger_run"},
}
EXPIRY_ACTION = {"reject": "reject", "exclude_instrument": "exclude", "keep_current": "reject"}
MAX_LEVEL_EDIT = 0.20          # an edited stop/target may move at most 20% from the quant level


class DecisionError(Exception):
    pass


@dataclass
class DecisionResult:
    task_id: int
    task_type: str
    action: str
    effects: list[str]
    status: str


def _f(x) -> Optional[float]:
    return None if x is None else float(x)


class HitlService:
    def __init__(self, factory: sessionmaker[Session], rr_floor: Optional[dict[str, float]] = None):
        self.f = factory
        self.rr_floor = rr_floor or {"short": 1.5, "medium": 2.0, "long": 1.5}

    # -- reading ---------------------------------------------------------------------
    def list_tasks(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        with self.f() as s:
            q = select(m.HitlTask)
            if status != "all":
                q = q.where(m.HitlTask.status == status)
            rows = s.scalars(q.order_by(m.HitlTask.priority, m.HitlTask.due_at, m.HitlTask.id).limit(limit)).all()
            return [self._dict(t) for t in rows]

    def get(self, task_id: int) -> dict[str, Any]:
        with self.f() as s:
            t = s.get(m.HitlTask, task_id)
            if t is None:
                raise DecisionError(f"task {task_id} not found")
            return self._dict(t)

    @staticmethod
    def _dict(t: m.HitlTask) -> dict[str, Any]:
        return {"id": t.id, "task_type": t.task_type, "subject_type": t.subject_type, "subject_id": t.subject_id,
                "priority": t.priority, "status": t.status, "reason": t.reason, "ai_recommendation": t.ai_recommendation,
                "payload": t.payload, "created_at": t.created_at.isoformat(),
                "due_at": t.due_at.isoformat() if t.due_at else None, "default_on_expiry": t.default_on_expiry,
                "allowed_actions": sorted(ACTIONS.get(t.task_type, set())), "decision": t.decision,
                "decided_by": t.decided_by, "decided_at": t.decided_at.isoformat() if t.decided_at else None,
                "decision_note": t.decision_note}

    # -- deciding --------------------------------------------------------------------
    def decide(self, task_id: int, action: str, *, decided_by: str = "owner", note: Optional[str] = None,
               edits: Optional[dict[str, Any]] = None, now: Optional[datetime] = None,
               _expired: bool = False) -> DecisionResult:
        now = now or datetime.now(timezone.utc)
        with self.f() as s, s.begin():
            t = s.get(m.HitlTask, task_id)
            if t is None:
                raise DecisionError(f"task {task_id} not found")
            if t.status != "pending":
                raise DecisionError(f"task {task_id} is already {t.status}")
            if action not in ACTIONS.get(t.task_type, set()):
                raise DecisionError(f"{action!r} is not allowed for {t.task_type}; use one of {sorted(ACTIONS.get(t.task_type, []))}")
            effects = getattr(self, f"_apply_{t.task_type}")(s, t, action, edits or {}, now)
            t.status = "expired" if _expired else ("edited" if action in ("edit", "adjust_stop")
                                                  else "rejected" if action in ("reject", "dismiss") else "approved")
            t.decided_by = "system:expiry" if _expired else decided_by
            t.decided_at = now
            t.decision = {"action": action, "edits": edits or {}, "ai_recommendation": t.ai_recommendation,
                          "effects": effects}
            t.decision_note = note
            return DecisionResult(t.id, t.task_type, action, effects, t.status)

    def expire_due(self, now: Optional[datetime] = None) -> list[DecisionResult]:
        """Apply the fail-safe default to overdue tasks (§8.3 'default on expiry')."""
        now = now or datetime.now(timezone.utc)
        out = []
        with self.f() as s:
            due = s.scalars(select(m.HitlTask).where(m.HitlTask.status == "pending", m.HitlTask.due_at.is_not(None),
                                                     m.HitlTask.due_at <= now)).all()
            due = [(t.id, t.task_type, t.default_on_expiry) for t in due]
        for tid, ttype, default in due:
            if default == "keep_and_remind":
                with self.f() as s, s.begin():
                    t = s.get(m.HitlTask, tid)
                    t.due_at = now + timedelta(days=1)
                    t.decision = {"reminders": (t.decision or {}).get("reminders", 0) + 1}
                out.append(DecisionResult(tid, ttype, "remind", ["kept open; reminder tomorrow"], "pending"))
                continue
            if default == "shadow_run":
                with self.f() as s, s.begin():
                    t = s.get(m.HitlTask, tid)
                    t.status, t.decided_at, t.decided_by = "expired", now, "system:expiry"
                    t.decision = {"action": "shadow_run", "effects": ["shadow re-evaluation requested (no publish)"]}
                out.append(DecisionResult(tid, ttype, "shadow_run", ["shadow re-evaluation requested"], "expired"))
                continue
            action = EXPIRY_ACTION.get(default, "reject")
            if action not in ACTIONS.get(ttype, set()):
                action = "reject" if "reject" in ACTIONS.get(ttype, set()) else sorted(ACTIONS[ttype])[0]
            out.append(self.decide(tid, action, now=now, _expired=True, note=f"expired; default {default}"))
        return out

    # -- effects per task type ---------------------------------------------------------
    def _reco(self, s: Session, t: m.HitlTask) -> m.Recommendation:
        r = s.get(m.Recommendation, t.subject_id) if t.subject_type == "recommendation" else None
        if r is None:
            raise DecisionError(f"task {t.id} has no recommendation subject")
        return r

    def _apply_approve_reco(self, s: Session, t: m.HitlTask, action: str, edits: dict, now: datetime) -> list[str]:
        r = self._reco(s, t)
        if action == "reject":
            r.status = "rejected"
            r.close_reason = "rejected in review"
            return ["recommendation rejected; not published"]
        effects = []
        if action == "edit":
            effects += self._edit_levels(r, edits)
        r.status = "published"
        r.published_at = now
        return effects + ["recommendation published"]

    def _edit_levels(self, r: m.Recommendation, edits: dict) -> list[str]:
        allowed = {"stop", "target"}
        bad = set(edits) - allowed - {"thesis_note"}
        if bad:
            raise DecisionError(f"only stop/target may be edited (got {sorted(bad)})")
        new = {k: float(edits[k]) for k in allowed if k in edits}
        stop = new.get("stop", _f(r.stop))
        target = new.get("target", _f(r.target))
        for k, v in new.items():
            orig = _f(getattr(r, k))
            if orig and abs(v / orig - 1) > MAX_LEVEL_EDIT:
                raise DecisionError(f"{k} {v} moves more than {MAX_LEVEL_EDIT:.0%} from the quant level {orig}")
        if not (stop < float(r.entry_low) <= float(r.entry_high) < target):
            raise DecisionError("edited levels must keep stop < entry_low <= entry_high < target")
        rr = (target - float(r.entry_high)) / (float(r.entry_high) - stop)
        if rr < self.rr_floor.get(r.horizon, 1.5):
            raise DecisionError(f"edited reward:risk {rr:.2f} is below the {r.horizon} floor")
        changes = []
        card = dict(r.card or {})
        levels = dict(card.get("levels") or {})
        for k, v in new.items():
            changes.append(f"{k} {_f(getattr(r, k))} -> {v}")
            setattr(r, k, Decimal(str(v)))
            levels[k] = v
        card["levels"] = levels
        card["human_edits"] = changes
        r.card = card
        r.risk_reward = Decimal(f"{rr:.3f}")
        return changes

    def _apply_resolve_conflict(self, s, t, action, edits, now) -> list[str]:
        r = self._reco(s, t)
        if action == "reject":
            r.status = "rejected"
            return ["recommendation rejected"]
        if action == "neutral":
            r.verdict, r.action = "neutral", "watch"
            r.status = "rejected"
            return ["downgraded to neutral; not published"]
        r.status, r.published_at = "published", now
        return ["conflict resolved in favour of the card; published"]

    def _apply_data_anomaly(self, s, t, action, edits, now) -> list[str]:
        sym = t.payload.get("symbol")
        return [f"{sym} excluded for {t.payload.get('day')}"] if action == "exclude" else [f"{sym} data accepted"]

    def _apply_review_thesis_break(self, s, t, action, edits, now) -> list[str]:
        r = s.get(m.Recommendation, t.subject_id) if t.subject_type == "recommendation" else None
        if action == "close":
            if r is not None:
                r.status, r.closed_at, r.close_reason = "invalidated", now, "thesis break confirmed in review"
            return ["recommendation invalidated; simulated positions exit at the next close"]
        if action == "adjust_stop":
            if "stop" not in edits:
                raise DecisionError("adjust_stop needs edits.stop")
            new_stop = Decimal(str(float(edits["stop"])))
            n = 0
            if r is not None:
                r.stop = new_stop
                for pos in s.scalars(select(m.Position).where(m.Position.reco_id == r.id, m.Position.status == "open")):
                    pos.stop = new_stop
                    n += 1
            return [f"stop moved to {float(new_stop)} on the recommendation and {n} open position(s)"]
        return ["no change"]

    def _apply_approve_config(self, s, t, action, edits, now) -> list[str]:
        v = s.get(m.ConfigVersion, t.subject_id)
        if v is None:
            raise DecisionError("config version not found")
        v.approved = action == "approve"
        return [f"config v{v.version} {'approved' if v.approved else 'rejected'}"]

    def _apply_approve_map_change(self, s, t, action, edits, now) -> list[str]:
        row = s.get(m.SectorSensitivity, t.subject_id)
        if row is None:
            raise DecisionError("map row not found")
        proposal = (t.payload or {}).get("proposal", {})
        kind = proposal.get("action", "add")
        old = s.get(m.SectorSensitivity, proposal["current_row_id"]) if proposal.get("current_row_id") else None
        if action == "reject":
            row.status = "retired"
            return ["map change rejected; current map kept"]
        if kind == "retire":
            row.status = "retired"
            if old is not None:
                old.status = "retired"
            return [f"map row {old.id if old else '?'} retired"]
        if action == "edit" and "magnitude" in edits:
            row.magnitude = Decimal(str(min(max(float(edits["magnitude"]), 0.0), 0.8)))
        row.status, row.approved_by, row.approved_at = "approved", "owner", now
        effects = [f"map row {row.id} approved (magnitude {float(row.magnitude)})"]
        if old is not None and old.id != row.id:
            old.status = "retired"
            effects.append(f"replaces row {old.id}")
        return effects

    def _apply_high_severity_event(self, s, t, action, edits, now) -> list[str]:
        e = s.get(m.Event, t.subject_id)
        if e is None:
            raise DecisionError("event not found")
        if action == "dismiss":
            e.status = "resolved"
            return ["event dismissed"]
        if action == "downgrade":
            e.severity = max(1, e.severity - 1)
            e.status = "active"
            return [f"severity lowered to {e.severity}"]
        e.status = "active"
        return ["event confirmed"] + (["event-triggered run requested"] if action == "trigger_run" else [])
