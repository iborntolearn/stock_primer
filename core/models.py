"""Database schema (design doc §3), portable across SQLite (now) and Postgres (later).

Deviations from the doc's DDL, all for portability:
- arrays (TEXT[], BIGINT[]) are JSON lists;
- JSONB is JSON on SQLite and JSONB on Postgres;
- news_item.embedding is a JSON list until pgvector is available;
- a few JSON `details`/`meta` columns hold agent fields the orchestrator records
  (event transmission hypotheses, recommendation violations/critique, regime inputs).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric,
    SmallInteger, String, Text, TypeDecorator, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PK = BigInteger().with_variant(Integer, "sqlite")   # SQLite only autoincrements INTEGER PRIMARY KEY
JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """Always tz-aware UTC in Python; SQLite stores naive UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; pass a tz-aware value")
        value = value.astimezone(timezone.utc)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: Optional[datetime], dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


Money = Numeric(18, 4)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType, datetime: UTCDateTime, Decimal: Money}


# ---------------------------------------------------------------- 3.3 reference & market data
class Sector(Base):
    __tablename__ = "sector"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sector.id"))


class Instrument(Base):
    __tablename__ = "instrument"
    __table_args__ = (UniqueConstraint("exchange", "symbol"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text)
    exchange: Mapped[str] = mapped_column(Text, default="NSE")
    isin: Mapped[Optional[str]] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    sector_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sector.id"))
    industry: Mapped[Optional[str]] = mapped_column(Text)
    listing_date: Mapped[Optional[date]] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class UniverseMembership(Base):
    __tablename__ = "universe_membership"
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), primary_key=True)
    universe: Mapped[str] = mapped_column(Text, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[Optional[date]] = mapped_column(Date)


class PriceDaily(Base):
    __tablename__ = "price_daily"
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    high: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    low: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    close: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    adj_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    prev_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    volume: Mapped[Optional[int]] = mapped_column(BigInteger)
    traded_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    delivery_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(default=utcnow)


class IndexPriceDaily(Base):
    __tablename__ = "index_price_daily"
    index_code: Mapped[str] = mapped_column(Text, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    high: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    low: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    close: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    total_return_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    pe: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    source: Mapped[str] = mapped_column(Text, default="nse_index_bhavcopy")
    ingested_at: Mapped[datetime] = mapped_column(default=utcnow)


class CorporateAction(Base):
    __tablename__ = "corporate_action"
    __table_args__ = (UniqueConstraint("instrument_id", "action_type", "ex_date", "source"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    action_type: Mapped[str] = mapped_column(Text)   # split|bonus|dividend|rights|buyback|merger
    ex_date: Mapped[date] = mapped_column(Date)
    ratio_num: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    ratio_den: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6))
    amount_per_share: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    details: Mapped[Optional[dict[str, Any]]]
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(default=utcnow)


class FundamentalSnapshot(Base):
    __tablename__ = "fundamental_snapshot"
    __table_args__ = (UniqueConstraint("instrument_id", "period_end", "period_type", "source"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    period_end: Mapped[date] = mapped_column(Date)
    period_type: Mapped[str] = mapped_column(Text)
    published_at: Mapped[date] = mapped_column(Date)
    revenue: Mapped[Optional[Decimal]]
    ebitda: Mapped[Optional[Decimal]]
    pat: Mapped[Optional[Decimal]]
    eps: Mapped[Optional[Decimal]]
    total_debt: Mapped[Optional[Decimal]]
    equity: Mapped[Optional[Decimal]]
    roce: Mapped[Optional[Decimal]]
    roe: Mapped[Optional[Decimal]]
    op_margin: Mapped[Optional[Decimal]]
    promoter_holding: Mapped[Optional[Decimal]]
    pledged_pct: Mapped[Optional[Decimal]]
    raw: Mapped[Optional[dict[str, Any]]]
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(default=utcnow)


class MacroSeries(Base):
    __tablename__ = "macro_series"
    series_id: Mapped[str] = mapped_column(Text, primary_key=True)
    obs_date: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal]
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(default=utcnow)


class FlowsDaily(Base):
    __tablename__ = "flows_daily"
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    fii_net: Mapped[Optional[Decimal]]
    dii_net: Mapped[Optional[Decimal]]
    source: Mapped[str] = mapped_column(Text)


class TradingCalendar(Base):
    __tablename__ = "trading_calendar"
    cal_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_trading_day: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[Optional[str]] = mapped_column(Text)


# ---------------------------------------------------------------- 3.4 news, events, transmission map
class NewsItem(Base):
    __tablename__ = "news_item"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    url_hash: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    snippet: Mapped[Optional[str]] = mapped_column(Text)
    body_ref: Mapped[Optional[str]] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(index=True)
    ingested_at: Mapped[datetime] = mapped_column(default=utcnow)
    language: Mapped[Optional[str]] = mapped_column(Text)
    provider_sentiment: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4))
    dedupe_cluster_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    embedding: Mapped[Optional[list[Any]]]


class NewsEntityLink(Base):
    __tablename__ = "news_entity_link"
    news_id: Mapped[int] = mapped_column(ForeignKey("news_item.id"), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), primary_key=True)
    relevance: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4))
    method: Mapped[str] = mapped_column(Text)


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (CheckConstraint("severity BETWEEN 1 AND 5"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    event_key: Mapped[str] = mapped_column(Text, unique=True)
    event_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    geographies: Mapped[Optional[list[Any]]]
    severity: Mapped[int] = mapped_column(SmallInteger)
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    first_seen: Mapped[Optional[datetime]]
    last_updated: Mapped[Optional[datetime]]
    status: Mapped[str] = mapped_column(Text, default="active")   # active|unverified|fading|resolved
    source_news_ids: Mapped[Optional[list[Any]]]
    extracted_by_run: Mapped[Optional[int]] = mapped_column(ForeignKey("run.id"))
    details: Mapped[Optional[dict[str, Any]]]


class MacroFactor(Base):
    __tablename__ = "macro_factor"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    series_id: Mapped[Optional[str]] = mapped_column(Text)


class SectorSensitivity(Base):
    __tablename__ = "sector_sensitivity"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    factor_id: Mapped[int] = mapped_column(ForeignKey("macro_factor.id"))
    sector_id: Mapped[int] = mapped_column(ForeignKey("sector.id"))
    direction: Mapped[int] = mapped_column(SmallInteger)
    magnitude: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)        # proposed|approved|retired
    proposed_by: Mapped[Optional[str]] = mapped_column(Text)
    approved_by: Mapped[Optional[str]] = mapped_column(Text)
    approved_at: Mapped[Optional[datetime]]


class EventImpact(Base):
    __tablename__ = "event_impact"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"))
    sector_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sector.id"))
    instrument_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instrument.id"))
    direction: Mapped[int] = mapped_column(SmallInteger)
    magnitude: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    horizon: Mapped[Optional[str]] = mapped_column(Text)
    channel: Mapped[Optional[str]] = mapped_column(Text)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    evidence_ids: Mapped[Optional[list[Any]]]
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("run.id"))
    details: Mapped[Optional[dict[str, Any]]]     # basis, map_row_ids, confidence, decay_days


class RegimeSnapshot(Base):
    __tablename__ = "regime_snapshot"
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    regime: Mapped[str] = mapped_column(Text)
    nifty_trend: Mapped[Optional[str]] = mapped_column(Text)
    breadth: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    india_vix: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    fii_5d: Mapped[Optional[Decimal]]
    usdinr_chg_5d: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    brent_chg_5d: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    us10y: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    narrative: Mapped[Optional[str]] = mapped_column(Text)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("run.id"))
    details: Mapped[Optional[dict[str, Any]]]     # inputs + LLM assessment


# ---------------------------------------------------------------- 3.5 runs, evidence, outputs, recommendations
class Run(Base):
    __tablename__ = "run"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    run_type: Mapped[str] = mapped_column(Text)
    trigger: Mapped[Optional[str]] = mapped_column(Text)
    requested_by: Mapped[Optional[str]] = mapped_column(Text)
    decision_time: Mapped[datetime]
    status: Mapped[str] = mapped_column(Text)     # running|succeeded|degraded|failed|cancelled
    config_version: Mapped[Optional[int]] = mapped_column(Integer)
    strategy_version: Mapped[Optional[str]] = mapped_column(Text)
    prompt_bundle_version: Mapped[Optional[str]] = mapped_column(Text)
    budget_tokens_in: Mapped[Optional[int]] = mapped_column(BigInteger)
    budget_tokens_out: Mapped[Optional[int]] = mapped_column(BigInteger)
    spent_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[Optional[datetime]]
    error: Mapped[Optional[dict[str, Any]]]


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[Optional[str]] = mapped_column(Text)
    as_of: Mapped[datetime]
    payload: Mapped[dict[str, Any]]
    payload_hash: Mapped[str] = mapped_column(Text, index=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("run.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AgentOutput(Base):
    __tablename__ = "agent_output"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id"))
    agent: Mapped[str] = mapped_column(Text)
    subject_type: Mapped[Optional[str]] = mapped_column(Text)
    subject_id: Mapped[Optional[str]] = mapped_column(Text)
    output: Mapped[dict[str, Any]]
    schema_version: Mapped[Optional[str]] = mapped_column(Text)
    validation_status: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendation"
    __table_args__ = (
        CheckConstraint("horizon IN ('short','medium','long')"),
        Index("ix_reco_status_horizon_pub", "status", "horizon", "published_at"),
    )
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    horizon: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(Text)
    action: Mapped[Optional[str]] = mapped_column(Text)
    quant_score: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    agent_adjustment: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    final_score: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    entry_low: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    entry_high: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    stop: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    target: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    expected_holding_days: Mapped[Optional[int]] = mapped_column(Integer)
    risk_reward: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 3))
    thesis: Mapped[str] = mapped_column(Text, default="")
    bull_case: Mapped[Optional[list[Any]]]
    bear_case: Mapped[Optional[list[Any]]]
    key_risks: Mapped[Optional[list[Any]]]
    invalidation: Mapped[Optional[list[Any]]]
    regime_at_issue: Mapped[Optional[str]] = mapped_column(Text)
    event_ids: Mapped[Optional[list[Any]]]
    card: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(Text, default="draft")
    published_at: Mapped[Optional[datetime]]
    closed_at: Mapped[Optional[datetime]]
    close_reason: Mapped[Optional[str]] = mapped_column(Text)
    strategy_version: Mapped[Optional[str]] = mapped_column(Text)
    prompt_bundle_version: Mapped[Optional[str]] = mapped_column(Text)
    model_ids: Mapped[Optional[list[Any]]]
    meta: Mapped[Optional[dict[str, Any]]]     # run_type, violations, gaps, critique, contributions


class RecoEvidence(Base):
    __tablename__ = "reco_evidence"
    reco_id: Mapped[int] = mapped_column(ForeignKey("recommendation.id"), primary_key=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence.id"), primary_key=True)
    role: Mapped[Optional[str]] = mapped_column(Text)   # supports|contradicts|context


class Outcome(Base):
    __tablename__ = "outcome"
    reco_id: Mapped[int] = mapped_column(ForeignKey("recommendation.id"), primary_key=True)
    evaluated_at: Mapped[Optional[datetime]]
    ret_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    bench_ret_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    alpha_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    max_adverse_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    max_favourable_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    hit_target: Mapped[Optional[bool]] = mapped_column(Boolean)
    hit_stop: Mapped[Optional[bool]] = mapped_column(Boolean)
    days_held: Mapped[Optional[int]] = mapped_column(Integer)
    confidence_bucket: Mapped[Optional[str]] = mapped_column(Text)
    # published (taken) | human_rejected | expired (not decided in time): lets the scorecard test whether review adds value
    cohort: Mapped[str] = mapped_column(Text, default="published", server_default="published")


# ---------------------------------------------------------------- 3.6 portfolio simulation
class Portfolio(Base):
    __tablename__ = "portfolio"
    __table_args__ = (CheckConstraint("type IN ('manual','auto_follow','quant_shadow')"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    cash: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    benchmark: Mapped[str] = mapped_column(Text, default="NIFTY 500")
    rules: Mapped[Optional[dict[str, Any]]]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class PaperTrade(Base):
    __tablename__ = "paper_trade"
    __table_args__ = (CheckConstraint("side IN ('BUY','SELL')"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    reco_id: Mapped[Optional[int]] = mapped_column(ForeignKey("recommendation.id"))
    side: Mapped[str] = mapped_column(Text)
    qty: Mapped[int] = mapped_column(Integer)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    fill_date: Mapped[date] = mapped_column(Date)
    product: Mapped[str] = mapped_column(Text, default="delivery")
    brokerage: Mapped[Optional[Decimal]]
    stt: Mapped[Optional[Decimal]]
    exch_charges: Mapped[Optional[Decimal]]
    gst: Mapped[Optional[Decimal]]
    stamp_duty: Mapped[Optional[Decimal]]
    dp_charges: Mapped[Optional[Decimal]]
    slippage: Mapped[Optional[Decimal]]
    total_cost: Mapped[Optional[Decimal]]
    reason: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[Optional[str]] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PaperOrder(Base):
    """Pending simulated orders (not in the doc's DDL). Orders fill at the next session's open (§4.6)."""

    __tablename__ = "paper_order"
    __table_args__ = (CheckConstraint("side IN ('BUY','SELL')"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    reco_id: Mapped[Optional[int]] = mapped_column(ForeignKey("recommendation.id"))
    side: Mapped[str] = mapped_column(Text, default="BUY")
    qty: Mapped[Optional[int]] = mapped_column(Integer)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2))
    stop: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    target: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    horizon: Mapped[Optional[str]] = mapped_column(Text)
    placed_at: Mapped[datetime] = mapped_column(default=utcnow)
    status: Mapped[str] = mapped_column(Text, default="pending")   # pending|filled|rejected|cancelled|expired
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    reject_reason: Mapped[Optional[str]] = mapped_column(Text)
    fill_trade_id: Mapped[Optional[int]] = mapped_column(ForeignKey("paper_trade.id"))
    idempotency_key: Mapped[Optional[str]] = mapped_column(Text, unique=True)


class Position(Base):
    __tablename__ = "position"
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"), primary_key=True)
    opened_on: Mapped[date] = mapped_column(Date, primary_key=True)
    reco_id: Mapped[Optional[int]] = mapped_column(ForeignKey("recommendation.id"))
    horizon: Mapped[Optional[str]] = mapped_column(Text)
    qty: Mapped[int] = mapped_column(Integer)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    stop: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    target: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    expires_on: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default="open")


class DailySnapshot(Base):
    __tablename__ = "daily_snapshot"
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id"), primary_key=True)
    snap_date: Mapped[date] = mapped_column(Date, primary_key=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    holdings_value: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    nav: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    benchmark_nav: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2))
    day_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2))
    exposure: Mapped[Optional[dict[str, Any]]]


class CostConfig(Base):
    __tablename__ = "cost_config"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date)
    rates: Mapped[dict[str, Any]]


# ---------------------------------------------------------------- 3.7 governance & observability
class HitlTask(Base):
    __tablename__ = "hitl_task"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    task_type: Mapped[str] = mapped_column(Text)
    subject_type: Mapped[Optional[str]] = mapped_column(Text)
    subject_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    priority: Mapped[int] = mapped_column(SmallInteger, default=3)
    payload: Mapped[dict[str, Any]]
    ai_recommendation: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", index=True)
    decided_by: Mapped[Optional[str]] = mapped_column(Text)
    decision: Mapped[Optional[dict[str, Any]]]
    decision_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    due_at: Mapped[Optional[datetime]]
    decided_at: Mapped[Optional[datetime]]
    default_on_expiry: Mapped[str] = mapped_column(Text, default="reject")


class LlmCall(Base):
    __tablename__ = "llm_call"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("run.id"), index=True)
    agent: Mapped[Optional[str]] = mapped_column(Text)
    call_id: Mapped[Optional[str]] = mapped_column(Text)
    model_id: Mapped[Optional[str]] = mapped_column(Text)
    prompt_version: Mapped[Optional[str]] = mapped_column(Text)
    input_hash: Mapped[Optional[str]] = mapped_column(Text)
    request: Mapped[Optional[dict[str, Any]]]
    response: Mapped[Optional[dict[str, Any]]]
    tokens_in: Mapped[Optional[int]] = mapped_column(Integer)
    tokens_out: Mapped[Optional[int]] = mapped_column(Integer)
    cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 5))
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    validation_status: Mapped[Optional[str]] = mapped_column(Text)
    retries: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class ToolCall(Base):
    __tablename__ = "tool_call"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("run.id"), index=True)
    agent: Mapped[Optional[str]] = mapped_column(Text)
    server: Mapped[Optional[str]] = mapped_column(Text)
    tool: Mapped[str] = mapped_column(Text)
    args: Mapped[Optional[dict[str, Any]]]
    args_hash: Mapped[Optional[str]] = mapped_column(Text, index=True)
    result_ref: Mapped[Optional[int]] = mapped_column(ForeignKey("evidence.id"))
    ok: Mapped[bool] = mapped_column(Boolean)
    error_code: Mapped[Optional[str]] = mapped_column(Text)
    cache_hit: Mapped[Optional[bool]] = mapped_column(Boolean)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConfigVersion(Base):
    __tablename__ = "config_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config: Mapped[dict[str, Any]]
    created_by: Mapped[Optional[str]] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SecurityEvent(Base):
    __tablename__ = "security_event"
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("run.id"))
    agent: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(Text)
    news_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    flag: Mapped[Optional[str]] = mapped_column(Text)
    excerpt_hash: Mapped[Optional[str]] = mapped_column(Text)
    action_taken: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DataGateResult(Base):
    """Outcome of the 18:20 data-quality gate per trading date (not in the doc's DDL; backs data_quality_status)."""

    __tablename__ = "data_gate_result"
    decision_date: Mapped[date] = mapped_column(Date, primary_key=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    is_trading_day: Mapped[bool] = mapped_column(Boolean)
    issues: Mapped[list[Any]]
    excluded: Mapped[list[Any]]          # symbols excluded from today's run (anomalies)
    stats: Mapped[dict[str, Any]]
    checked_at: Mapped[datetime] = mapped_column(default=utcnow)


class SourceHealth(Base):
    __tablename__ = "source_health"
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(primary_key=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    last_data_at: Mapped[Optional[datetime]]
    note: Mapped[Optional[str]] = mapped_column(Text)


class AlertLog(Base):
    """Intraday alerts sent (Phase 5). One row per (day, kind, subject) so each alert fires once a day."""
    __tablename__ = "alert_log"
    __table_args__ = (UniqueConstraint("alert_date", "kind", "subject_key"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    alert_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)            # near_stop | stop_breach | target_hit
    subject_key: Mapped[str] = mapped_column(Text)     # e.g. "pos:3:INFY" or "reco:41"
    symbol: Mapped[str] = mapped_column(Text)
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    level: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))
    payload: Mapped[Optional[dict[str, Any]]]
    sent_at: Mapped[datetime] = mapped_column(default=utcnow)


class Shareholding(Base):
    """Quarterly shareholding pattern (SEBI LODR Reg. 31), aggregates only. Percentages are of total shares (0-100),
    except encumbered/pledged, which are % of the promoter group's own holding. published_at = exchange broadcast
    (a revision replaces the row and moves published_at forward, so nothing is known before it was filed)."""
    __tablename__ = "shareholding"
    __table_args__ = (UniqueConstraint("instrument_id", "period_end", "source"),)
    id: Mapped[int] = mapped_column(PK, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instrument.id"))
    period_end: Mapped[date] = mapped_column(Date)
    published_at: Mapped[datetime]
    promoter_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    public_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    fpi_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))          # foreign institutions
    dii_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))          # domestic institutions
    mf_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    insurance_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    retail_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))       # resident individuals
    government_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    promoter_encumbered_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    promoter_pledged_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 4))
    shareholders: Mapped[Optional[int]] = mapped_column(BigInteger)
    revised: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[Optional[dict[str, Any]]]
    source: Mapped[str] = mapped_column(Text, default="nse_shp")
    ingested_at: Mapped[datetime] = mapped_column(default=utcnow)
