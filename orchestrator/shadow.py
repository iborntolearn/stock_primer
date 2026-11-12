"""Quant-only shadow picks (§7.5 value-add test): the screen's top names with quant levels,
no agents and no critic. Fetched through the tool layer so they are audited like everything else."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .deps import Deps


async def quant_shadow_picks(deps: Deps, decision_time: datetime, horizon: str = "short", n: int | None = None,
                             run_id: int | None = None) -> list[dict[str, Any]]:
    n = n or deps.cfg.max_published_per_horizon
    r = await deps.tools.call("orchestrator", "screen_universe",
                              {"horizon": horizon, "top_k": n, "as_of": decision_time.isoformat()},
                              run_id=run_id, as_of=decision_time)
    picks = []
    for c in (r.data or {}).get("candidates", []) if r.ok else []:
        lv = await deps.tools.call("orchestrator", "propose_levels",
                                   {"symbol": c["symbol"], "horizon": horizon, "as_of": decision_time.isoformat()},
                                   run_id=run_id, as_of=decision_time)
        if lv.ok and lv.data:
            picks.append({"symbol": c["symbol"], "horizon": horizon, "stop": lv.data["stop"], "target": lv.data["target"],
                          "quant_score": c["quant_score"]})
    return picks
