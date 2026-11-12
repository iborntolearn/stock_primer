"""ToolGateway over MCP (§5): the orchestrator's client to the MCP servers.

    async with McpToolGateway.stdio(["market", "quant", ...]) as gw:
        deps.tools = AllowlistedTools(gw)

Connects to each server (stdio subprocess, or an in-process MCPServer in tests),
learns which server owns which tool, and passes the caller role, run id and
decision time with every call. Evidence is written by the server side.
"""
from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any, Optional

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from .ports import ToolError, ToolResult

INJECTED = {"caller_role", "run_id", "as_of"}


class McpToolGateway:
    def __init__(self, targets: dict[str, Any]):
        self._targets = targets          # server name -> MCPServer | StdioServerParameters
        self._stack: Optional[AsyncExitStack] = None
        self._clients: dict[str, Client] = {}
        self._route: dict[str, str] = {}
        self._specs: dict[str, dict[str, Any]] = {}

    @classmethod
    def stdio(cls, servers: list[str], python: str = sys.executable) -> "McpToolGateway":
        import os
        from pathlib import Path

        root = str(Path(__file__).resolve().parent.parent)
        # Pass the full environment so STOCK_PRIMER_* settings (database URL, ...) reach the servers.
        return cls({n: StdioServerParameters(command=python, args=["-m", "mcp_servers.server", "--name", n],
                                             cwd=root, env=dict(os.environ)) for n in servers})

    async def __aenter__(self) -> "McpToolGateway":
        self._stack = AsyncExitStack()
        for name, target in self._targets.items():
            client = await self._stack.enter_async_context(Client(target))
            self._clients[name] = client
            for t in (await client.list_tools()).tools:
                self._route[t.name] = name
                schema = dict(t.input_schema)
                props = {k: v for k, v in schema.get("properties", {}).items() if k not in INJECTED}
                schema["properties"] = props
                schema["required"] = [r for r in schema.get("required", []) if r not in INJECTED]
                schema.pop("title", None)
                self._specs[t.name] = {"name": t.name, "description": (t.description or t.name).split("\n\n")[0],
                                       "input_schema": schema}
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack:
            await self._stack.aclose()

    def tool_specs(self, names: frozenset[str]) -> list[dict[str, Any]]:
        return [self._specs[n] for n in sorted(names) if n in self._specs]

    async def call(self, role: str, tool: str, args: dict[str, Any], *, run_id: int, as_of: datetime) -> ToolResult:
        from mcp_servers.service import from_envelope

        server = self._route.get(tool)
        if server is None:
            return ToolResult(ok=False, error=ToolError("NOT_FOUND", f"no MCP server provides {tool}"))
        payload = {**args, "as_of": args.get("as_of") or as_of.isoformat(), "caller_role": role, "run_id": run_id}
        try:
            res = await self._clients[server].call_tool(tool, payload)
        except Exception as e:
            return ToolResult(ok=False, error=ToolError("SOURCE_DOWN", f"{type(e).__name__}: {e}", retryable=True))
        text = "".join(getattr(c, "text", "") for c in res.content)
        if res.is_error:
            return ToolResult(ok=False, error=ToolError("INVALID_ARGUMENT", text[:500]))
        try:
            return from_envelope(json.loads(text))
        except (ValueError, KeyError) as e:
            return ToolResult(ok=False, error=ToolError("SOURCE_DOWN", f"bad envelope: {e}"))
