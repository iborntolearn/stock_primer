"""Evidence collection and prompt fencing (§2.1, §5.3, §6.1).

Agents never see raw tool output. The orchestrator gathers it through the
allowlisted gateway, keeps an index of evidence IDs for validation, and renders
each item as an <evidence id="E123"> block. Payloads that carry external text
are wrapped in <untrusted_document> so the model treats them as data only.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .ports import ToolGateway, ToolResult


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str, ensure_ascii=False)


def escape_text(text: str) -> str:
    # Stop external text from closing our fence tags.
    return text.replace("</untrusted_document", "&lt;/untrusted_document").replace("</evidence", "&lt;/evidence")


@dataclass
class EvidenceItem:
    ref: str
    tool: str
    kind: str
    source: str
    as_of: datetime
    data: Any
    untrusted: bool

    def render(self) -> str:
        # UTC so a live run and its replay (as_of read back from the DB) render identical prompts.
        as_of = self.as_of.astimezone(timezone.utc).isoformat() if self.as_of.tzinfo else self.as_of.isoformat()
        head = f'id="{self.ref}" tool="{self.tool}" source="{self.source}" as_of="{as_of}"'
        body = escape_text(_compact(self.data))
        if self.untrusted:
            return f"<untrusted_document {head}>{body}</untrusted_document>"
        return f"<evidence {head}>{body}</evidence>"


@dataclass
class EvidenceBundle:
    """Evidence gathered for one agent call, plus what went missing."""

    items: list[EvidenceItem] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def add(self, tool: str, result: ToolResult) -> Any:
        if not result.ok or result.meta is None or result.evidence_ref is None:
            err = result.error.code if result.error else "NO_EVIDENCE"
            self.gaps.append(f"{tool}: {err}")
            return None
        m = result.meta
        if m.data_quality != "ok":
            self.gaps.append(f"{tool}: data_quality={m.data_quality}")
        self.items.append(EvidenceItem(result.evidence_ref, tool, m.kind, m.source, m.as_of, result.data, m.untrusted))
        return result.data

    def ids(self) -> frozenset[str]:
        return frozenset(i.ref for i in self.items)

    def render(self, tools: set[str] | None = None) -> str:
        chosen = [i for i in self.items if tools is None or i.tool in tools]
        return "\n".join(i.render() for i in chosen) or "<none/>"

    def by_ref(self) -> dict[str, EvidenceItem]:
        return {i.ref: i for i in self.items}

    def merge(self, other: "EvidenceBundle") -> "EvidenceBundle":
        return EvidenceBundle(self.items + other.items, self.gaps + other.gaps)

    def to_state(self) -> list[dict]:
        return [
            {"ref": i.ref, "tool": i.tool, "kind": i.kind, "source": i.source,
             "as_of": i.as_of.isoformat(), "data": i.data, "untrusted": i.untrusted}
            for i in self.items
        ]

    @classmethod
    def from_state(cls, rows: list[dict]) -> "EvidenceBundle":
        return cls([EvidenceItem(r["ref"], r["tool"], r["kind"], r["source"], datetime.fromisoformat(r["as_of"]),
                                 r["data"], r["untrusted"]) for r in rows])


async def gather(tools: ToolGateway, role: str, calls: list[tuple[str, dict]], *, run_id: int,
                 as_of: datetime) -> EvidenceBundle:
    """Run a fixed list of tool calls for an agent role; failures become data gaps, not exceptions."""
    bundle = EvidenceBundle()
    results = await asyncio.gather(
        *(tools.call(role, name, {**args, "as_of": as_of.isoformat()}, run_id=run_id, as_of=as_of) for name, args in calls),
        return_exceptions=True,
    )
    for (name, _), res in zip(calls, results):
        if isinstance(res, BaseException):
            bundle.gaps.append(f"{name}: {type(res).__name__}: {res}")
        else:
            bundle.add(name, res)
    return bundle


def as_json(obj: Any) -> str:
    """Render orchestrator-owned (trusted) JSON for a prompt section."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return _compact(obj)
