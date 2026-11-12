"""ToolService: runs registered tools with the §5.3 contract.

- role allowlist (§5.6) checked here too, not only by the orchestrator
- every successful result is stored as an evidence row; the envelope carries its ID
- every call is written to tool_call (args hash -> evidence, used by replay)
- errors come back as {code, message, retryable}, never as exceptions
It implements the orchestrator's ToolGateway port, so the orchestrator can call
tools in-process; mcp_servers.server exposes the same service over MCP.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import typing
import json
import time
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Optional

import pydantic

from core.store import SqlStore
from orchestrator.config import TOOL_ALLOWLIST
from orchestrator.ports import ToolError, ToolMeta, ToolResult

from .registry import TOOLS, Out, ToolContext, ToolFailure, decision_dt, register_all, set_context

INJECTED = ("as_of",)


def args_hash(tool: str, args: dict) -> str:
    clean = {k: v for k, v in args.items() if k not in ("as_of", "caller_role", "run_id")}
    return hashlib.sha256(json.dumps([tool, clean], sort_keys=True, default=str).encode()).hexdigest()


def _schema(fn) -> dict[str, Any]:
    """JSON schema of a tool's parameters (minus injected ones), for Claude's tool definitions."""
    fields = {}
    hints = typing.get_type_hints(fn)
    for name, p in inspect.signature(fn).parameters.items():
        if name in INJECTED:
            continue
        ann = hints.get(name, Any)
        fields[name] = (ann, ... if p.default is inspect.Parameter.empty else p.default)
    model = pydantic.create_model(f"{fn.__name__}_args", **fields)
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


class ToolService:
    def __init__(self, tool_ctx: ToolContext, store: SqlStore, allowlist: dict[str, frozenset[str]] = TOOL_ALLOWLIST):
        register_all()
        set_context(tool_ctx)
        self.ctx = tool_ctx
        self.store = store
        self.allow = allowlist
        # SQLite: serialise tool execution with the store's lock (one writer, shared test connection).
        self._lock = getattr(store, "_lock", None) or nullcontext()

    def tool_specs(self, names: frozenset[str]) -> list[dict[str, Any]]:
        specs = []
        for n in sorted(names):
            d = TOOLS.get(n)
            if d is None:
                continue
            doc = (inspect.getdoc(d.fn) or n).split("\n\n")[0]
            specs.append({"name": n, "description": doc, "input_schema": _schema(d.fn)})
        return specs

    def servers(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for d in TOOLS.values():
            out.setdefault(d.server, []).append(d.name)
        return out

    def _run(self, tool: str, args: dict) -> Out:
        with self._lock:
            return TOOLS[tool].fn(**args)

    async def call(self, role: str, tool: str, args: dict[str, Any], *, run_id: Optional[int], as_of: datetime) -> ToolResult:
        t0 = time.perf_counter()
        args = {k: v for k, v in args.items() if k not in ("caller_role", "run_id")}
        args["as_of"] = args.get("as_of") or as_of.isoformat()
        d = TOOLS.get(tool)
        result: ToolResult
        if d is None:
            result = ToolResult(ok=False, error=ToolError("NOT_FOUND", f"unknown tool {tool}"))
        elif tool not in self.allow.get(role, frozenset()):
            result = ToolResult(ok=False, error=ToolError("FORBIDDEN", f"role {role!r} may not call {tool!r}"))
        else:
            try:
                out = await asyncio.to_thread(self._run, tool, args)
                src_as_of = out.as_of or decision_dt(args["as_of"])
                eid = self.store.add_evidence(out.kind, out.source, src_as_of, out.data, run_id,
                                              source_ref=f"{d.server}-mcp:{tool}")
                result = ToolResult(ok=True, data=out.data, meta=ToolMeta(
                    evidence_id=eid, source=out.source, as_of=src_as_of, kind=out.kind,
                    data_quality=out.data_quality, warnings=out.warnings, untrusted=out.untrusted))
            except ToolFailure as e:
                result = ToolResult(ok=False, error=ToolError(e.code, e.message, e.retryable, e.retry_after_s))
            except TypeError as e:
                result = ToolResult(ok=False, error=ToolError("INVALID_ARGUMENT", str(e)))
            except Exception as e:   # a broken source must not crash the agent
                result = ToolResult(ok=False, error=ToolError("SOURCE_DOWN", f"{type(e).__name__}: {e}", retryable=True))
        self.store.record_tool_call({
            "run_id": run_id, "agent": role, "server": d.server if d else None, "tool": tool,
            "args": {k: v for k, v in args.items()}, "args_hash": args_hash(tool, args),
            "result_ref": result.meta.evidence_id if result.ok and result.meta else None, "ok": result.ok,
            "error_code": result.error.code if result.error else None,
            "latency_ms": int((time.perf_counter() - t0) * 1000)})
        return result


def envelope(r: ToolResult) -> dict[str, Any]:
    """§5.3 wire format."""
    if r.ok and r.meta:
        return {"ok": True, "data": r.data, "error": None, "meta": {
            "evidence_id": r.meta.evidence_id, "source": r.meta.source, "as_of": r.meta.as_of.isoformat(),
            "kind": r.meta.kind, "cache_hit": r.meta.cache_hit, "data_quality": r.meta.data_quality,
            "warnings": r.meta.warnings, "untrusted": r.meta.untrusted}}
    e = r.error or ToolError("UNKNOWN", "")
    return {"ok": False, "data": None, "meta": None, "error": {
        "code": e.code, "message": e.message, "retryable": e.retryable, "retry_after_s": e.retry_after_s}}


def from_envelope(env: dict[str, Any]) -> ToolResult:
    if env.get("ok"):
        mt = env["meta"]
        return ToolResult(ok=True, data=env["data"], meta=ToolMeta(
            evidence_id=mt["evidence_id"], source=mt["source"], as_of=datetime.fromisoformat(mt["as_of"]),
            kind=mt.get("kind", "calc"), cache_hit=mt.get("cache_hit", False),
            data_quality=mt.get("data_quality", "ok"), warnings=mt.get("warnings", []),
            untrusted=mt.get("untrusted", False)))
    e = env.get("error") or {}
    return ToolResult(ok=False, error=ToolError(e.get("code", "UNKNOWN"), e.get("message", ""),
                                                e.get("retryable", False), e.get("retry_after_s")))
