// Shapes returned by the FastAPI backend (api/main.py).

export type Horizon = 'short' | 'medium' | 'long';
export type Verdict = 'favourable' | 'neutral' | 'avoid';

export interface Levels { entry_low: number; entry_high: number; stop: number; target: number; }
export interface Claim { text: string; evidence_ids: string[]; }

export interface Card {
  symbol: string; horizon: Horizon; verdict: Verdict; action: string;
  headline: string; thesis: string; bull_case: string[]; bear_case: string[];
  key_risks: { risk: string; evidence_ids: string[] }[]; what_to_watch: string[];
  levels: Levels; invalidation: { condition: string; machine_checkable: boolean }[];
  confidence: number; confidence_rationale: string; claims: Claim[]; human_edits?: string[];
}

export interface Outcome { ret_pct: number; alpha_pct: number | null; hit_target: boolean; hit_stop: boolean; days_held: number; }

export interface RecoRow {
  id: number; symbol: string; horizon: Horizon; verdict: Verdict; action: string | null; status: string;
  quant_score: number; final_score: number; confidence: number | null;
  entry_low: number | null; entry_high: number | null; stop: number | null; target: number | null;
  risk_reward: number | null; headline: string | null; run_id: number; run_type: string | null;
  published_at: string | null; closed_at: string | null; close_reason: string | null; outcome: Outcome | null;
}

export interface EvidenceRef { id: string; kind: string; source: string; as_of: string; role: string | null; source_ref: string | null; }
export interface Violation { code: string; message: string; action: string; }

export interface RecoDetail extends RecoRow {
  card: Card; thesis: string; bull_case: string[]; bear_case: string[]; invalidation: Card['invalidation'];
  regime_at_issue: string | null; violations: Violation[]; gaps: string[]; critique: any;
  evidence: EvidenceRef[];
  agents: Record<string, any>;
  contributions: Contribution[];
  run: { id: number; run_type: string; decision_time: string; config_version: number; prompt_bundle_version: string; strategy_version: string } | null;
}

export interface Contribution { source: string; points: number; evidence_ids: string[]; }

export interface Evidence { id: string; kind: string; source: string; source_ref: string | null; as_of: string; payload: any; untrusted: boolean; }

export interface Digest {
  date: string; attention: string[]; market: string; picks: { symbol: string; horizon: Horizon; lines: string }[];
  no_pick_notes: string[]; sim?: string; text: string; reco_ids: number[];
}

export interface Health { status: string; last_price_date: string | null; last_gate: { date: string; passed: boolean } | null; kill_switch: boolean; }

export interface Regime {
  as_of: string; regime: string; score: number | null; inputs: Record<string, any>;
  assessment: { narrative?: string; contradictions?: string[]; implications?: Record<Horizon, string> };
}

export interface Metrics {
  nav: number; total_return_pct: number; benchmark_return_pct: number | null; alpha_pct: number | null;
  xirr_pct: number | null; max_drawdown_pct: number; volatility_ann_pct: number | null; closed_trades: number;
  hit_rate_pct: number | null; avg_win_pct: number | null; avg_loss_pct: number | null; costs_paid: number; days: number;
}

export interface PortfolioSummary {
  id: number; name: string; type: string; starting_cash: number; cash: number; benchmark: string;
  metrics: Metrics | null; open_positions: number;
}

export interface NavPoint { date: string; nav: number; benchmark_nav: number | null; }

export interface Position {
  symbol: string; horizon: Horizon | null; qty: number; avg_cost: number; last: number; pnl_pct: number;
  stop: number | null; target: number | null; opened_on: string; expires_on: string | null; reco_id: number | null;
}

export interface PortfolioDetail {
  portfolio_id: number; name: string; type: string; starting_cash: number; cash: number; benchmark: string;
  nav_series: NavPoint[]; metrics: Metrics | null; rules: Record<string, any>; positions: Position[];
}

export interface Trade { id: number; symbol: string; side: 'BUY' | 'SELL'; qty: number; price: number; date: string; reason: string; costs: number | null; reco_id: number | null; }
export interface Order { id: number; symbol: string; status: string; qty: number | null; amount: number | null; stop: number | null; target: number | null; placed_at: string; reject_reason: string | null; reco_id: number | null; }
export interface OrderResult { order_id: number; symbol: string; status: string; expected_fill: string; stop: number | null; target: number | null; }

export interface HitlTask {
  id: number; task_type: string; subject_type: string | null; subject_id: number | null; priority: number;
  status: string; reason: string | null; ai_recommendation: string | null; payload: any; created_at: string;
  due_at: string | null; default_on_expiry: string; allowed_actions: string[]; decision: any;
  decided_by: string | null; decided_at: string | null; decision_note: string | null;
}

export interface RunRow { id: number; run_type: string; trigger: string | null; status: string; decision_time: string; spent_usd: number | null; started_at: string; finished_at: string | null; }
export interface RunDetail {
  id: number; run_type: string; trigger: string | null; status: string; error: any; decision_time: string;
  spent_usd: number | null; config_version: number | null; prompt_bundle_version: string | null;
  tool_calls: { tool: string; agent: string; ok: boolean; error_code: string | null; evidence: string | null; latency_ms: number }[];
  llm_calls: { call_id: string; agent: string; model: string; tokens_in: number; tokens_out: number; cost_usd: number; latency_ms: number; validation: string; retries: number }[];
  recommendations: number[];
}

export interface JobEvent { seq: number; kind: string; message: string; run_id?: number; node?: string; }
export interface AnalysisResult {
  symbol: string; horizon: Horizon; verdict: Verdict; final_score: number | null; confidence: number; status: string;
  violations: Violation[]; gaps: string[]; card: Card | null; reco_id: number | null;
}
export interface Job {
  job_id: number; kind: string; status: 'running' | 'succeeded' | 'failed'; events: JobEvent[]; error: string | null;
  result: { run_id: number; clarification: string | null; results: AnalysisResult[]; errors: string[] } | null;
}

export interface ScoreSummary {
  n: number; sufficient_sample: boolean; positive_alpha_rate: number | null; target_hit_rate: number | null;
  stop_hit_rate: number | null; avg_return_pct: number | null; avg_alpha_pct: number | null; avg_days_held: number | null;
}
export interface Scorecard {
  min_sample: number; overall: ScoreSummary; by_horizon: Record<string, ScoreSummary>;
  calibration: (ScoreSummary & { bucket: string; mean_confidence: number })[];
  abstention: { runs: number; runs_without_picks: number; rate: number | null };
  cost: { llm_spend_usd: number; published: number; usd_per_published: number | null };
  portfolios: { portfolio_id: number; name: string; type: string; metrics: Metrics | null; nav_series: NavPoint[] }[];
  attribution: {
    by_sector: Record<string, ScoreSummary>; by_regime: Record<string, ScoreSummary>;
    by_agent: { source: string; raised: ScoreSummary; lowered: ScoreSummary }[];
  };
  human_review: Record<string, ScoreSummary>;
  value_add: {
    auto_follow_return_pct: number; quant_shadow_return_pct: number; agents_minus_quant_pp: number;
    auto_follow_trades: number; quant_shadow_trades: number; sufficient_sample: boolean;
  } | null;
}

// Phase 4: grounded chat
export interface ChatResult { run_id: number; intent: string | null; reply: string; citations: string[]; results: AnalysisResult[]; }
export interface ChatJob { job_id: number; kind: string; status: 'running' | 'succeeded' | 'failed'; events: JobEvent[]; error: string | null; result: ChatResult | null; }

// Phase 3: market & events
export interface MacroSeries { points: { date: string; value: number }[]; last: number | null; chg_1d: number | null; chg_5d: number | null; unit: '%' | 'pp'; }
export interface MacroResponse { series: Record<string, MacroSeries>; flows: { date: string; fii_net_cr: number | null; dii_net_cr: number | null }[]; }
export interface SectorRow { sector: string; index: string | null; ret_5d_pct: number | null; ret_1m_pct: number | null; ret_3m_pct: number | null; event_overlay: number; }
export interface CalendarItem { date: string; symbol: string | null; kind: string; detail: string | null; open_pick: boolean; }
export interface MapRow {
  id: number; factor: string; factor_name: string; sector: string; direction: number; magnitude: number;
  rationale: string | null; status: string; version: number; approved_by: string | null; needs_review: boolean;
}
export interface EventImpactView { sector: string; direction: number; magnitude: number; horizon: string | null; channel: string | null; basis?: string; }
export interface MarketEvent {
  id: number; event_key: string; event_type: string; title: string; summary: string | null; severity: number;
  status: string; confidence: number | null; geographies: string[] | null; impacts: EventImpactView[];
  market_relevance_to_india?: string; first_seen?: string;
}
export interface PremarketBrief {
  date: string; text: string; attention: string[]; pending_reviews: number;
  macro: { series: string; label: string; last: number; change: number; unit: string }[];
  flows: { date: string; fii_net_cr: number; dii_net_cr: number } | null;
  events: { event_id: number; title: string; severity: number; status: string }[];
}

// Phase 5: what-if / stress, comparison, alerts
export interface Shock { factor: string; change: number; }
export interface StressPart { factor: string; per_unit: number; t: number; points: number; used?: boolean; }
export interface MapCheck { factor: string; map_direction: number; map_magnitude: number; rationale: string | null; agreement: string; }
export interface StressSector { sector: string; index: string; move_pct: number; weeks: number; parts: StressPart[]; map: MapCheck[]; }
export interface StressHolding {
  symbol: string; sector: string | null; qty?: number; last?: number; value: number; move_pct: number; pnl: number;
  est_price?: number; basis: string; stop: number | null; breaches_stop: boolean; weight_pct?: number | null;
  target?: number | null; reaches_target?: boolean;
}
export interface StressPortfolio {
  portfolio_id: number; name: string; type: string; cash: number; nav: number; invested?: number; pnl: number;
  pnl_pct: number | null; benchmark_pct?: number | null; stops_breached: number; holdings: StressHolding[];
}
export interface StressPick { reco_id: number; symbol: string; sector: string | null; horizon: string; last: number; stop: number | null; move_pct: number; est_price: number; basis: string; breaches_stop: boolean; }
export interface StressResult {
  as_of: string; scenario: { name: string; shocks: Shock[] };
  market: { move_pct: number; basis: string; links: { factor: string; per_unit: number; t: number; used: boolean }[] };
  sectors: StressSector[]; portfolios: StressPortfolio[]; open_picks: StressPick[]; assumptions: string[]; notes: string[];
}
export interface ReplayResult {
  as_of: string; window: { start: string; end: string; benchmark_pct: number; trading_days: number };
  sectors: { sector: string; move_pct: number }[]; portfolios: StressPortfolio[]; assumptions: string[];
}
export interface StressPresets {
  as_of: string | null; presets: { id: string; name: string; shocks: Shock[] }[];
  factors: { code: string; label: string; unit: string }[]; latest: Record<string, number>;
}

export interface CompareItem {
  symbol: string; name: string; sector: string | null;
  prices: {
    last: number; ret_1m_pct: number | null; ret_3m_pct: number | null; ret_6m_pct: number | null; ret_1y_pct: number | null;
    vol_ann_pct: number | null; max_drawdown_1y_pct: number; range_52w_position: number | null; high_52w: number; low_52w: number;
    atr_pct: number | null; avg_traded_value_cr: number | null; beta: number | null; above_200dma: boolean | null;
  } | null;
  fundamentals: Record<string, any> | null;
  shareholding: Record<string, any> | null;
  scores: Record<string, { quant_score: number; gates_passed: boolean; failed_gates: string[]; rank: number | null; universe_size: number } | null>;
  recommendations: Record<string, { reco_id: number; verdict: string; status: string; confidence: number | null; final_score: number | null; date: string }>;
}
export interface CompareResponse { as_of: string; benchmark: string; items: CompareItem[]; rebased: Record<string, { date: string; value: number }[]>; }

export interface AlertRow { id: number; date: string; kind: string; symbol: string; subject: string; price: number | null; level: number | null; label: string | null; sent_at: string; }
