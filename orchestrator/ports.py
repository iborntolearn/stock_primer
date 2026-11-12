"""Interfaces the orchestrator depends on.

The real implementations live elsewhere (MCP servers §5, Postgres §3, the
notification service §10). `orchestrator.fakes` provides in-memory versions so
the graphs can run and be tested before those exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol, TypeVar

from pydantic import BaseModel

from .config import TOOL_ALLOWLIST

M = TypeVar("M", bound=BaseModel)


@dataclass
class ToolMeta:
    evidence_id: Optional[int]
    source: str
    as_of: datetime
    kind: str = "calc"
    cache_hit: bool = False
    data_quality: str = "ok"          # ok | degraded | stale
    warnings: list[str] = field(default_factory=list)
    untrusted: bool = False           # payload carries external free text


@dataclass
class ToolError:
    code: str                         # RATE_LIMITED | NOT_FOUND | STALE_DATA | SOURCE_DOWN | ...
    message: str
    retryable: bool = False
    retry_after_s: Optional[int] = None


@dataclass
class ToolResult:
    """§5.3 response envelope."""

    ok: bool
    data: Any = None
    meta: Optional[ToolMeta] = None
    error: Optional[ToolError] = None

    @property
    def evidence_ref(self) -> Optional[str]:
        if self.meta and self.meta.evidence_id is not None:
            return f"E{self.meta.evidence_id}"
        return None


class ToolForbidden(Exception):
    pass


class ToolGateway(Protocol):
    """Typed access to MCP tools. Every successful call is stored as evidence."""

    async def call(self, role: str, tool: str, args: dict[str, Any], *, run_id: int, as_of: datetime) -> ToolResult: ...

    def tool_specs(self, names: frozenset[str]) -> list[dict[str, Any]]:
        """Anthropic tool definitions (name/description/input_schema) for the given tools."""
        ...


class AllowlistedTools:
    """Wraps a gateway and enforces the §5.6 per-agent allowlist on the orchestrator side.

    The MCP servers enforce it again with a role claim; this is the first line.
    """

    def __init__(self, inner: ToolGateway, allowlist: dict[str, frozenset[str]] = TOOL_ALLOWLIST):
        self._inner = inner
        self._allow = allowlist

    async def call(self, role: str, tool: str, args: dict[str, Any], *, run_id: int, as_of: datetime) -> ToolResult:
        if tool not in self._allow.get(role, frozenset()):
            raise ToolForbidden(f"role {role!r} may not call {tool!r}")
        return await self._inner.call(role, tool, args, run_id=run_id, as_of=as_of)

    def tool_specs(self, names: frozenset[str]) -> list[dict[str, Any]]:
        return self._inner.tool_specs(names)


@dataclass
class EvidenceRow:
    id: int
    kind: str
    source: str
    as_of: datetime
    payload: Any
    run_id: int


class Store(Protocol):
    """System of record (§3). Append-only for runs, calls and recommendations."""

    # runs
    def create_run(self, run_type: str, decision_time: datetime, trigger: str, config_version: int,
                   strategy_version: str, prompt_bundle_version: str) -> int: ...
    def finish_run(self, run_id: int, status: str, spent_usd: float, error: Optional[dict] = None) -> None: ...
    def find_run(self, run_type: str, decision_date: str) -> Optional[int]: ...

    # evidence
    def get_evidence(self, evidence_id: int) -> Optional[EvidenceRow]: ...

    # audit
    def record_llm_call(self, row: dict) -> None: ...
    def record_security_event(self, row: dict) -> None: ...
    def save_agent_output(self, run_id: int, agent: str, subject_type: str, subject_id: str,
                          output: dict, validation_status: str, confidence: Optional[float]) -> None: ...

    # market context
    def save_regime(self, as_of: str, row: dict) -> None: ...
    def latest_regime(self) -> Optional[dict]: ...
    def save_event(self, event: dict) -> int: ...
    def event_exists(self, event_key: str) -> bool: ...
    def save_event_impacts(self, event_id: int, impacts: dict, run_id: int) -> None: ...
    def active_events(self, since: datetime) -> list[dict]: ...

    # recommendations
    def save_recommendation(self, reco: dict) -> int: ...
    def set_reco_status(self, reco_id: int, status: str, **fields: Any) -> None: ...
    def recent_reco(self, symbol: str, horizon: str, since: datetime) -> Optional[dict]: ...
    def open_recos(self, sectors: Optional[set[str]] = None) -> list[dict]: ...

    # HITL
    def create_hitl_task(self, task_type: str, subject_type: str, subject_id: Optional[int], payload: dict,
                         ai_recommendation: str, reason: str, priority: int = 3,
                         default_on_expiry: str = "reject", due_at: Optional[datetime] = None) -> int: ...


class Notifier(Protocol):
    async def send(self, channel: str, kind: str, text: str, payload: Optional[dict] = None) -> None: ...


class LLM(Protocol):
    """What agents need from the model layer (implemented by `llm.ClaudeLLM`)."""

    async def structured(self, *, call_id: str, agent: str, run_id: int, decision_time: datetime,
                         sections: dict[str, str], output_model: type[M], cite_ids: frozenset[str] = frozenset(),
                         template_vars: Optional[dict[str, str]] = None, images: Optional[list[bytes]] = None) -> M: ...

    async def tool_answer(self, *, call_id: str, agent: str, run_id: int, decision_time: datetime,
                          question: str, tools: ToolGateway, role: str) -> tuple[str, list[str]]: ...
