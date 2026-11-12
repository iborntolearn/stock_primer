"""Deterministic score combination (§4.4.2). The LLM nudges; it never decides.

final_score = quant_score + clamp(sum(bounded, evidence-backed contributions), -A, +A)
The verdict comes from score bands and hard gates, not from any agent.
"""
from __future__ import annotations

from typing import Any, Optional

from .config import Config
from .llm import cited_ids


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def combine(quant: dict, views: dict[str, Optional[dict]], critique: Optional[dict], cfg: Config,
            extra_gaps: int = 0) -> dict[str, Any]:
    contributions = []
    for name, view in views.items():
        if not view:
            continue
        adj = float(view.get("score_adjustment", 0.0))
        if view.get("security_flag"):
            adj = 0.0  # V12: content from a flagged source does not move the score
        ids = sorted(cited_ids(view))
        if not ids:
            adj = 0.0  # every contribution must cite evidence
        contributions.append({"source": name, "points": round(adj * cfg.analyst_points_per_unit, 3),
                              "evidence_ids": ids})
    if critique:
        contributions.append({"source": "critic",
                              "points": round(-float(critique.get("score_penalty", 0.0)) * cfg.critic_points_per_unit, 3),
                              "evidence_ids": sorted(cited_ids(critique))})

    raw = sum(c["points"] for c in contributions)
    adjustment = _clamp(raw, -cfg.adjustment_cap, cfg.adjustment_cap)
    quant_score = float(quant["quant_score"])
    final = quant_score + adjustment

    gates_passed = bool(quant.get("gates_passed", True))
    if not gates_passed:
        verdict = "avoid"   # the LLM can never lift a stock over a hard gate
    elif final >= cfg.favourable_min_score:
        verdict = "favourable"
    elif final >= cfg.neutral_min_score:
        verdict = "neutral"
    else:
        verdict = "avoid"

    gaps = extra_gaps + sum(len(v.get("data_gaps", [])) for v in views.values() if v)
    conf = float(quant.get("confidence", 0.5))
    if critique:
        conf -= float(critique.get("confidence_penalty", 0.0))
    conf -= gaps * cfg.data_gap_confidence_penalty
    return {
        "quant_score": quant_score,
        "agent_adjustment": round(adjustment, 3),
        "final_score": round(final, 3),
        "contributions": contributions,
        "verdict": verdict,
        "gates_passed": gates_passed,
        "max_confidence": round(_clamp(conf, 0.0, 1.0), 3),
        "data_gap_count": gaps,
    }
