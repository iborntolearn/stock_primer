"""Structured output schemas for every LLM call (design doc §6.4).

These are passed to `messages.parse(output_format=...)`, so the API constrains
the JSON shape and pydantic re-validates ranges afterwards (V1).
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

UnitF = Annotated[float, Field(ge=0, le=1)]
SignedF = Annotated[float, Field(ge=-1, le=1)]
EvidenceIds = list[str]


# LLM-01 -------------------------------------------------------------------
class RouteConstraints(BaseModel):
    sector: Optional[str] = None
    max_ideas: Optional[int] = None


class RouteDecision(BaseModel):
    intent: Literal[
        "analyze_stock", "discover_ideas", "explain_pick", "market_context",
        "portfolio_question", "add_to_portfolio", "settings", "other",
    ]
    symbols: list[str]
    horizon: Optional[Literal["short", "medium", "long", "all"]] = None
    constraints: RouteConstraints
    needs_clarification: bool
    clarifying_question: Optional[str] = None


# LLM-02 -------------------------------------------------------------------
class TransmissionHypothesis(BaseModel):
    channel: Literal["crude", "fx", "rates", "trade", "sentiment", "supply_chain", "regulation", "demand", "other"]
    description: str


class ExtractedEvent(BaseModel):
    event_key: str
    event_type: Literal[
        "geopolitical", "monetary_policy", "commodity", "trade_policy", "regulatory",
        "macro_data", "disaster", "corporate_sector", "other",
    ]
    title: str
    summary: str
    geographies: list[str]
    severity: int = Field(ge=1, le=5)
    severity_rationale: str
    market_relevance_to_india: Literal["none", "low", "medium", "high"]
    transmission_hypotheses: list[TransmissionHypothesis]
    source_news_ids: list[int]
    confidence: UnitF
    security_flag: Optional[str] = None


class EventList(BaseModel):
    events: list[ExtractedEvent]


# LLM-03 -------------------------------------------------------------------
class SectorImpact(BaseModel):
    sector: str
    direction: Literal[-1, 1]
    magnitude: UnitF
    horizon: Literal["short", "medium", "long"]
    channel: str
    rationale: str
    basis: Literal["approved_map", "llm_inference"]
    map_row_ids: list[int]
    evidence_ids: EvidenceIds
    confidence: UnitF


class StockImpact(BaseModel):
    symbol: str
    direction: Literal[-1, 1]
    reason: str
    evidence_ids: EvidenceIds


class EventImpacts(BaseModel):
    event_id: int
    impacts: list[SectorImpact]
    stock_specific: list[StockImpact]
    decay_days: int
    data_gaps: list[str]


# LLM-04 -------------------------------------------------------------------
class RegimeDriver(BaseModel):
    factor: str
    reading: str
    evidence_ids: EvidenceIds


class HorizonImplications(BaseModel):
    short: str
    medium: str
    long: str


class RegimeAssessment(BaseModel):
    regime_label_confirmed: Literal["risk_on", "neutral", "risk_off"]
    agrees_with_quant_label: bool
    drivers: list[RegimeDriver]
    contradictions: list[str]
    narrative: str
    implications: HorizonImplications


# LLM-05 -------------------------------------------------------------------
class LeadingSector(BaseModel):
    sector: str
    reason: str
    evidence_ids: EvidenceIds
    confidence: UnitF


class LaggingSector(BaseModel):
    sector: str
    reason: str
    evidence_ids: EvidenceIds


class Theme(BaseModel):
    name: str
    horizon: Literal["medium", "long"]
    supporting_events: list[int]
    evidence_ids: EvidenceIds


class SectorOverlay(BaseModel):
    sector: str
    overlay_score: SignedF


class SectorView(BaseModel):
    leading_sectors: list[LeadingSector]
    lagging_sectors: list[LaggingSector]
    themes: list[Theme]
    sector_overlay: list[SectorOverlay]


# LLM-06 -------------------------------------------------------------------
RedFlag = Literal["fraud", "regulatory", "default", "governance", "litigation", "guidance_cut", "none"]


class TriageItem(BaseModel):
    symbol: str
    keep: bool
    red_flags: list[RedFlag]
    reason: str


class TriageResult(BaseModel):
    items: list[TriageItem]


# LLM-07 -------------------------------------------------------------------
class Point(BaseModel):
    point: str
    evidence_ids: EvidenceIds


class KeyLevels(BaseModel):
    support: list[float]
    resistance: list[float]
    evidence_ids: EvidenceIds


class TechnicalView(BaseModel):
    symbol: str
    horizon: Literal["short", "medium", "long"]
    trend: Literal["up", "sideways", "down"]
    setup: Literal["breakout", "pullback", "base", "extended", "breakdown", "none"]
    supporting_points: list[Point]
    concerns: list[Point]
    key_levels: KeyLevels
    stance: Literal["constructive", "neutral", "cautious"]
    score_adjustment: SignedF
    confidence: UnitF
    data_gaps: list[str]


# LLM-08 -------------------------------------------------------------------
class Assessment(BaseModel):
    assessment: Literal["strong", "adequate", "weak"]
    points: list[Point]


class Valuation(BaseModel):
    assessment: Literal["cheap", "fair", "rich"]
    vs: Literal["sector", "history", "both"]
    points: list[Point]


class GovFlag(BaseModel):
    flag: str
    severity: Literal["low", "med", "high"]
    evidence_ids: EvidenceIds


class BalanceSheetGov(BaseModel):
    flags: list[GovFlag]


class FundamentalView(BaseModel):
    symbol: str
    horizon: Literal["medium", "long"]
    quality: Assessment
    growth: Assessment
    valuation: Valuation
    balance_sheet_and_governance: BalanceSheetGov
    earnings_trajectory: Literal["improving", "stable", "deteriorating", "unclear"]
    score_adjustment: SignedF
    confidence: UnitF
    data_gaps: list[str]


# LLM-09 -------------------------------------------------------------------
class Development(BaseModel):
    summary: str
    type: Literal["results", "order_win", "regulatory", "management", "capex", "litigation", "corporate_action", "sector", "other"]
    impact: Literal["positive", "negative", "mixed", "neutral"]
    materiality: Literal["low", "med", "high"]
    evidence_ids: EvidenceIds


class Catalyst(BaseModel):
    what: str
    date: Optional[str] = None
    evidence_ids: EvidenceIds


class EventExposure(BaseModel):
    event_id: int
    link: str


class NewsView(BaseModel):
    symbol: str
    recent_developments: list[Development]
    upcoming_catalysts: list[Catalyst]
    sentiment: SignedF
    event_exposure: list[EventExposure]
    score_adjustment: SignedF
    confidence: UnitF
    security_flag: Optional[str] = None


# LLM-10 -------------------------------------------------------------------
class Counterargument(BaseModel):
    argument: str
    severity: Literal["low", "med", "high"]
    evidence_ids: EvidenceIds


class UnsupportedClaim(BaseModel):
    claim: str
    where: str


class InvalidationCondition(BaseModel):
    condition: str
    machine_checkable: bool
    metric: Optional[str] = None
    threshold: Optional[str] = None


class CritiqueReport(BaseModel):
    symbol: str
    horizon: str
    verdict: Literal["proceed", "proceed_with_caution", "reject", "needs_human"]
    strongest_counterarguments: list[Counterargument]
    unsupported_claims: list[UnsupportedClaim]
    inconsistencies: list[str]
    missing_checks: list[str]
    base_rate_note: str
    confidence_penalty: float = Field(ge=0, le=0.5)
    score_penalty: UnitF
    invalidation_conditions_suggested: list[InvalidationCondition]


# LLM-11 -------------------------------------------------------------------
class Risk(BaseModel):
    risk: str
    evidence_ids: EvidenceIds


class Levels(BaseModel):
    entry_low: float
    entry_high: float
    stop: float
    target: float


class Claim(BaseModel):
    text: str
    evidence_ids: EvidenceIds


class RecommendationCard(BaseModel):
    symbol: str
    horizon: Literal["short", "medium", "long"]
    verdict: Literal["favourable", "neutral", "avoid"]
    action: Literal["watch", "accumulate", "enter_on_pullback", "avoid"]
    headline: str
    thesis: str
    bull_case: list[str]
    bear_case: list[str]
    key_risks: list[Risk]
    what_to_watch: list[str]
    levels: Levels
    invalidation: list[InvalidationCondition]
    confidence: UnitF
    confidence_rationale: str
    claims: list[Claim]
    disclaimer_key: Literal["decision_support_v1"]


# LLM-12 -------------------------------------------------------------------
class ChangeSinceEntry(BaseModel):
    what: str
    evidence_ids: EvidenceIds


class PositionReview(BaseModel):
    symbol: str
    status: Literal["on_track", "watch", "thesis_at_risk", "thesis_broken"]
    changes_since_entry: list[ChangeSinceEntry]
    suggested_action: Literal["hold", "review_stop", "trim_candidate", "exit_candidate"]
    reason: str


class EventExposureSymbols(BaseModel):
    event_id: int
    exposed_symbols: list[str]


class PortfolioReview(BaseModel):
    portfolio_id: int
    as_of: str
    positions: list[PositionReview]
    concentration_flags: list[str]
    event_exposure: list[EventExposureSymbols]
    summary: str


# LLM-16 -------------------------------------------------------------------
class DigestPick(BaseModel):
    symbol: str
    horizon: Literal["short", "medium", "long"]
    lines: str


class Digest(BaseModel):
    attention: list[str]
    market: str
    picks: list[DigestPick]
    no_pick_notes: list[str]


# LLM-13 -------------------------------------------------------------------
class MapProposal(BaseModel):
    action: Literal["add", "modify", "retire"]
    factor: str
    sector: str
    direction: Literal[-1, 1]
    magnitude: UnitF
    rationale: str
    supporting_event_ids: list[int]
    supporting_outcome_ids: list[int]
    expected_confidence: UnitF


class MapChangeProposals(BaseModel):
    proposals: list[MapProposal]


# LLM-15 -------------------------------------------------------------------
class Line(BaseModel):
    value: Optional[float] = None
    page: Optional[int] = None


class StatementLines(BaseModel):
    period_end: str
    nature: Literal["consolidated", "standalone", "unclear"]
    currency_unit: str
    revenue: Line
    other_income: Line
    total_income: Line
    profit_before_tax: Line
    tax_expense: Line
    net_profit: Line
    net_profit_owners: Line
    eps_basic: Line
    paid_up_capital: Line
    face_value: Line
    extraction_confidence: UnitF
    unreadable_fields: list[str]
