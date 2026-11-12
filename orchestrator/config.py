"""Versioned orchestrator configuration (design doc §4.4, §4.5, §4.8, §6.1, §8.2).

Everything a human might want to tune lives here: model tiers, budgets, fan-out
limits, score bands, autonomy levels. Models are configuration, not code.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, Field

Horizon = Literal["short", "medium", "long"]
HORIZONS: tuple[Horizon, ...] = ("short", "medium", "long")
Tier = Literal["small", "medium", "large"]


class Autonomy(IntEnum):
    """§8.2 autonomy levels."""

    L0_MANUAL = 0
    L1_APPROVE_EACH = 1
    L2_APPROVE_EXCEPTIONS = 2
    L3_AUDIT_SAMPLING = 3


class ModelTier(BaseModel):
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    max_tokens: int
    # Server-side refusal fallback (beta). Supported on Opus 5.5 / Sonnet 5.5, not Haiku.
    refusal_fallback: bool = False


class Pricing(BaseModel):
    input_per_mtok: float
    output_per_mtok: float


class Config(BaseModel):
    version: int = 1
    strategy_version: str = "quant-v2"          # load_config overrides with quant.factors.STRATEGY_VERSION
    prompt_bundle_version: str = "bundle-v1"

    tiers: dict[Tier, ModelTier] = {
        "small": ModelTier(model="claude-haiku-5-5", effort="low", max_tokens=8000),
        "medium": ModelTier(model="claude-sonnet-5-5", effort="medium", max_tokens=16000, refusal_fallback=True),
        "large": ModelTier(model="claude-opus-5-5", effort="high", max_tokens=16000, refusal_fallback=True),
    }
    pricing: dict[str, Pricing] = {
        "claude-haiku-5-5": Pricing(input_per_mtok=0.10, output_per_mtok=0.50),
        "claude-sonnet-5-5": Pricing(input_per_mtok=2.00, output_per_mtok=10.00),
        "claude-opus-5-5": Pricing(input_per_mtok=4.00, output_per_mtok=20.00),
    }
    # call_id -> tier (§6.2)
    call_tiers: dict[str, Tier] = {
        "LLM-01": "small", "LLM-02": "small", "LLM-03": "medium", "LLM-04": "medium",
        "LLM-05": "medium", "LLM-06": "small", "LLM-07": "medium", "LLM-08": "medium",
        "LLM-09": "medium", "LLM-10": "large", "LLM-11": "large", "LLM-12": "medium",
        "LLM-13": "medium", "LLM-14": "medium", "LLM-15": "small", "LLM-16": "medium",
    }
    # call_id -> prompt version file (prompts/<call_id>/<version>.md)
    prompt_versions: dict[str, str] = {}

    # Budgets (§4.8): USD per run and per day; 80% soft warning, 100% hard stop.
    budget_usd_per_run: float = 3.0
    budget_usd_per_day: float = 6.0
    budget_usd_per_month: float = 100.0
    budget_soft_fraction: float = 0.8

    # Fan-out (§4.5.2)
    top_k_per_horizon: int = 15
    top_k_degraded: int = 5
    max_published_per_horizon: int = 5
    research_concurrency: int = 6
    node_timeout_s: float = 120.0

    # Scoring (§4.4.2). quant_score is assumed normalised to 0..100 by the quant engine.
    adjustment_cap: float = 15.0                  # A = 15% of the 0..100 range
    analyst_points_per_unit: float = 5.0          # score_adjustment of 1.0 -> 5 points
    critic_points_per_unit: float = 10.0          # score_penalty of 1.0 -> -10 points
    favourable_min_score: float = 65.0
    neutral_min_score: float = 45.0
    data_gap_confidence_penalty: float = 0.05

    # Levels (§4.4.3): minimum reward:risk per horizon
    rr_floor: dict[Horizon, float] = {"short": 1.5, "medium": 2.0, "long": 1.5}

    # Publishing / HITL (§8)
    autonomy: dict[str, Autonomy] = {
        "publish_short": Autonomy.L1_APPROVE_EACH,
        "publish_medium": Autonomy.L1_APPROVE_EACH,
        "publish_long": Autonomy.L1_APPROVE_EACH,
    }
    publish_confidence_min: float = 0.55
    kill_switch: bool = False
    republish_cooldown_days: int = 5

    # Validation (§7)
    max_evidence_age_days: dict[str, int] = {
        "price_series": 3, "indicator": 3, "calc": 3, "macro": 7, "flows": 7,
        "event": 14, "news": 30, "filing": 200, "fundamental": 200,
    }
    numeric_tolerance: dict[str, float] = {"price": 0.0005, "ratio": 0.01, "pct_pp": 0.1}
    high_severity_threshold: int = 4
    event_trigger_severity: int = 4
    news_lookback_days: int = 14

    # Response cache (§4.8): identical (call, prompt version, inputs) within this window reuse the answer
    llm_cache_hours: float = 20.0

    # Intraday stop-proximity alerts (Phase 5, optional): polled quotes, alerts only; simulated fills stay end-of-day
    intraday_alerts: bool = False
    stop_proximity_pct: float = 1.5

    # Q&A tool loop
    qa_max_iterations: int = 8

    universe: str = "NIFTY500"
    benchmark: str = "NIFTY500"
    extra: dict = Field(default_factory=dict)

    def autonomy_for(self, horizon: Horizon) -> Autonomy:
        if self.kill_switch:
            return Autonomy.L0_MANUAL
        return self.autonomy.get(f"publish_{horizon}", Autonomy.L0_MANUAL)

    def tier_for(self, call_id: str) -> ModelTier:
        return self.tiers[self.call_tiers[call_id]]

    def cost_usd(self, model: str, tokens_in: int, tokens_out: int) -> float:
        p = self.pricing.get(model)
        if p is None:
            return 0.0
        return tokens_in / 1e6 * p.input_per_mtok + tokens_out / 1e6 * p.output_per_mtok


# §5.6 per-agent tool allowlist. "orchestrator" is the non-LLM role that screens and scores.
TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "regime": frozenset({"get_regime_inputs", "get_index_history", "get_market_breadth", "get_macro_series", "get_flows"}),
    "event_extraction": frozenset({"search_news", "get_world_events", "get_world_news"}),
    "event_impact": frozenset({"get_event", "get_sector_sensitivities", "get_macro_series", "get_index_history"}),
    "sector": frozenset({"get_index_history", "get_world_events"}),
    "technical": frozenset({"get_price_history", "compute_indicators", "get_index_history", "propose_levels"}),
    "fundamental": frozenset({"get_financials", "get_ratios", "get_valuation", "get_shareholding", "search_filings", "get_filing_excerpt"}),
    "news": frozenset({"get_stock_news", "search_news", "get_event_calendar", "get_corporate_actions"}),
    "critic": frozenset({"get_evidence", "search_recommendations", "search_news", "get_price_history"}),
    "synthesizer": frozenset(),
    "map_proposer": frozenset({"get_sector_sensitivities", "get_index_history", "get_world_events", "get_event"}),
    "portfolio_review": frozenset({"get_portfolio", "get_exposure", "get_stock_news", "get_price_history", "get_event_calendar"}),
    "qa": frozenset({
        "resolve_instrument", "get_price_history", "get_quote_snapshot", "get_index_history", "get_universe",
        "get_corporate_actions", "get_market_breadth", "get_trading_calendar", "get_financials", "get_ratios",
        "get_valuation", "get_shareholding", "search_filings", "get_filing_excerpt", "get_stock_news",
        "search_news", "get_world_events", "get_event", "get_event_calendar", "get_macro_series", "get_flows",
        "get_regime_inputs", "get_sector_sensitivities", "compute_indicators", "get_factor_scores", "get_world_news",
        "list_portfolios", "get_portfolio", "get_performance", "get_exposure", "get_evidence",
        "search_recommendations",
    }),
    "orchestrator": frozenset({
        "resolve_instrument", "get_universe", "get_trading_calendar", "get_regime_inputs", "get_world_events",
        "get_event", "get_index_history", "screen_universe", "score_horizon", "propose_levels",
        "get_factor_scores", "get_stock_news", "get_event_calendar", "get_corporate_actions", "search_news",
        "data_quality_status", "get_portfolio", "list_portfolios", "get_price_history",
    }),
}
