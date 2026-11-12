"""Expose the tool service over MCP (stdio).

    python -m mcp_servers.server --name market      # one of: market quant macro news fundamentals evidence portfolio all

Each tool's MCP signature is its Python signature plus two injected parameters,
`caller_role` and `run_id`. The role is a claim from the (local, single-user)
orchestrator; the service enforces the §5.6 allowlist with it. Results are the
§5.3 envelope as JSON.
"""
from __future__ import annotations

import argparse
import functools
import inspect
import typing
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from .registry import TOOLS, register_all
from .service import ToolService, envelope


def _wrap(service: ToolService, name: str):
    fn = TOOLS[name].fn
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)   # tool modules use string annotations

    async def handler(**kwargs: Any) -> dict:
        from .registry import decision_dt

        role = kwargs.pop("caller_role", "")
        run_id = kwargs.pop("run_id", None)
        as_of = decision_dt(kwargs.get("as_of", ""))
        return envelope(await service.call(role, name, kwargs, run_id=run_id, as_of=as_of))

    params = [p.replace(annotation=hints.get(p.name, Any)) for p in sig.parameters.values()] + [
        inspect.Parameter("caller_role", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str),
        inspect.Parameter("run_id", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Optional[int]),
    ]
    params = [p.replace(kind=inspect.Parameter.KEYWORD_ONLY) for p in params]
    handler.__signature__ = inspect.Signature(params, return_annotation=dict)
    handler.__name__ = name
    handler.__doc__ = fn.__doc__
    handler.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict}
    return handler


def build_server(service: ToolService, name: str = "all") -> MCPServer:
    register_all()
    srv = MCPServer(f"stock-primer-{name}", instructions="Indian equity research data and compute. Decision support only.")
    for tool_name, d in sorted(TOOLS.items()):
        if name in ("all", d.server):
            srv.add_tool(_wrap(service, tool_name), name=tool_name, description=(d.fn.__doc__ or "").strip())
    return srv


def default_service() -> ToolService:
    from core.db import session_factory
    from core.net import setup_tls
    from core.settings import get_settings
    from core.store import SqlStore
    from ingestion.adapters.gnews import GoogleNews
    from quant.engine import QuantEngine

    from .registry import ToolContext

    setup_tls()
    f = session_factory()
    settings = get_settings()
    return ToolService(ToolContext(factory=f, quant=QuantEngine(f, settings), settings=settings, news=GoogleNews()),
                       SqlStore(f))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="all")
    args = p.parse_args()
    build_server(default_service(), args.name).run()


if __name__ == "__main__":
    main()
