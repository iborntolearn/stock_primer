# stock_primer — LangGraph orchestrator

The orchestrator from the *Agentic Stock Research & Recommendation Platform* design doc (v0.1): it runs the agents, enforces budgets and timeouts, validates every LLM output, and routes anything risky to a human. It is **decision support only, not investment advice**, and it never places orders.

## Layout

| Path | What it is | Doc section |
|---|---|---|
| `orchestrator/config.py` | Model tiers, budgets, fan-out limits, score bands, autonomy levels, per-agent tool allowlist | §4.4, §4.8, §5.6, §8.2 |
| `orchestrator/schemas.py` | Pydantic output schema for every LLM call | §6.4 |
| `orchestrator/prompts/` | Shared preamble + versioned task prompts (`LLM-07/v1.md`) | §6.3, §6.4 |
| `orchestrator/llm.py` | Claude client: structured output, one repair retry, refusal handling, cost + audit log, Q&A tool loop | §6.1, §6.6 |
| `orchestrator/agents.py` | The 12 agents + triage and digest tasks | §4.5.1 |
| `orchestrator/scoring.py` | Deterministic `final_score = quant + clamp(adjustment)` and verdict bands | §4.4.2 |
| `orchestrator/validation.py` | Card checks V2–V9, V11, V12 | §7.1 |
| `orchestrator/graphs/` | LangGraph graphs (below) | §4.5 |
| `orchestrator/ports.py` | Interfaces for MCP tools, store, notifier, LLM | §3, §5 |
| `orchestrator/fakes.py` | In-memory store, fake MCP tools (synthetic data), scripted LLM | — |

## Graphs

- **candidate**: per-stock research. Runs the technical, fundamental and news analysts in parallel, then combine → critic → synthesize → validate → record.
- **daily**: data gate, then regime and events in parallel, then sector → screen → triage. Each candidate fans out (`Send`) to the candidate graph, then select (autonomy / HITL) → digest → finalize. Also serves the weekly and monthly runs via `horizons`.
- **on_demand**: `/analyze` and "what should I look at now?". Never publishes.
- **chat**: query router, then one of clarify, analyze, discover, Q&A tool loop, or unsupported.
- **events**: 07:15 event digest (extract → store → impacts) and the event-triggered shadow re-evaluation.
- **portfolio**: portfolio review. Its suggestions become HITL tasks.

Print any graph as Mermaid: `python -m orchestrator.cli graph daily`.

## Running

```bash
pip install -e .[dev]
python -m pytest
python -m orchestrator.cli daily --horizons short,medium
python -m orchestrator.cli analyze HDFCBANK
python -m orchestrator.cli chat "what should I look at now?"
```

Every command runs on synthetic fake data until the MCP servers and Postgres exist. Add `--live-llm` to call Claude for real; this needs `ANTHROPIC_API_KEY` (or `ant auth login`) and costs money.

## Data foundations (Phase 0)

| Path | What it is |
|---|---|
| `core/models.py` | All design-doc tables (§3), portable SQLite ↔ Postgres; plus `data_gate_result` |
| `core/store.py` | `SqlStore`, the database implementation of the orchestrator's `Store` port |
| `migrations/` | Alembic (`render_as_batch` for SQLite) |
| `ingestion/adapters/` | NSE bhavcopy, index bhavcopy, holidays, corporate actions, Nifty constituents, yfinance fallback |
| `ingestion/eod.py` | EOD load → split/bonus adjustment → reconciliation (anomalies become `data_anomaly` HITL tasks) |
| `ingestion/quality.py` | 18:20 data-quality gate; fails closed |

```bash
stock-primer-data init-db && stock-primer-data seed
stock-primer-data calendar --year 2026
stock-primer-data universe --since 2026-09-01   # --since only for the first snapshot (see note)
stock-primer-data backfill --start 2026-09-28
stock-primer-data eod          # daily, after ~18:00 IST
stock-primer-data gate
stock-primer-data status
```

The database defaults to `data/stock_primer.db` (SQLite). For Postgres, set `STOCK_PRIMER_DATABASE_URL=postgresql+psycopg://...` and install `.[postgres]`. Raw downloads are cached under `data/cache/`.

**Universe history:** the free constituent list is a current snapshot only. `--since` backdates the first snapshot, so dates before it carry survivorship bias. From then on, membership changes are tracked point-in-time.

## Single-stock analysis on real data (Phase 1)

| Path | What it is |
|---|---|
| `quant/` | Indicators, price-led factor library, horizon scores (0–100 percentile), hard gates, levels on the 0.05 tick, regime label |
| `mcp_servers/` | Tools for market, quant, macro, news, fundamentals, evidence and portfolio. `ToolService` handles the envelope, evidence, `tool_call` audit and allowlist. `server.py` exposes them over MCP (stdio) |
| `orchestrator/mcp_gateway.py` | The orchestrator's MCP client |
| `orchestrator/wiring.py` | Builds the real dependencies (`real_deps`): SQL store, tools (in-process or MCP), Claude, notifier |
| `orchestrator/replay.py` | Re-runs a recorded analysis from stored evidence and LLM responses, and diffs levels and verdicts |
| `bot/` | Owner-only Telegram bot with `/analyze` |
| `core/net.py` | Trusts the Windows certificate store, needed behind HTTPS-inspecting networks |

```bash
python -m orchestrator.cli --env local analyze HDFCBANK                       # live Claude (needs ANTHROPIC_API_KEY)
python -m orchestrator.cli --env local --llm scripted analyze HDFCBANK        # pipeline check, no cost
python -m orchestrator.cli --env local --transport mcp analyze HDFCBANK       # tools over MCP subprocesses
python -m orchestrator.cli replay <run_id>                                    # no network, no cost
python -m bot.main                                                            # needs the Telegram settings in .env
```

**Price-led phase:**
- Fundamentals tools return NOT_FOUND, so the Fundamental Analyst records a data gap and makes no LLM call.
- Quality, growth, valuation, event and news factors score 0 and lower confidence.
- The regime uses Nifty 500 trend, breadth and India VIX. FII flows, USD/INR, Brent and US yields come in Phase 3.

## Daily loop, simulation and dashboard (Phase 2)

| Path | What it is |
|---|---|
| `simulator/` | Paper portfolios. Fills at the next session's open with slippage, liquidity, position and sector caps. Delivery costs come from `cost_config`. Exits on stop, target, horizon or thesis break (gaps fill at the open). Applies splits, bonuses and dividends. NAV vs benchmark, XIRR, drawdown, hit rate. The auto-follow portfolio takes every published pick |
| `evaluation/` | Scores published picks at stop, target or horizon end vs Nifty 500. Scorecard: calibration, abstention rate, cost per pick; nothing is judged below 20 samples |
| `hitl/` | Review-inbox decisions and their effects. Stop/target edits are allowed within guard ranges. Expired tasks get the safe default (picks are not published) |
| `orchestrator/digest.py` | The 19:30 digest, built after review decisions |
| `scheduler/` | APScheduler jobs, IST. 18:00 pipeline: ingest (retries until 20:00), then gate, then daily run, then review tasks. 19:15 overdue alert; 19:30 digest + auto-follow; 23:00 simulator + outcome scoring; Saturday medium and first-Saturday long runs; expiry every 5 minutes |
| `api/` | FastAPI (§4.7) with SSE progress for analyses. Bearer token if `STOCK_PRIMER_API_TOKEN` is set. `api/demo.py` serves a synthetic demo |
| `bot/` | Telegram: `/analyze /picks /inbox /portfolio /pause /resume`, plus approve/reject buttons (owner chat only) |
| `web/` | Angular 21 dashboard: Today, Recommendations, detail with evidence drawer, Analyze (streamed), Review inbox, Portfolios with NAV chart, Scorecard, Runs & audit, Settings (kill switch, token, theme) |

```bash
python -m api.main                      # API on :8000 (real database, live Claude)
python -m api.demo                      # or: API on :8000 over a SYNTHETIC demo database (data/demo.db)
cd web && npm start                     # dashboard on http://localhost:4200 (proxies /api to :8000)
python -m scheduler.main                # the daily schedule; --once evening|digest|evaluation to run one job
python -m bot.main                      # Telegram bot
```

On this network npm needs `NODE_EXTRA_CA_CERTS=<repo>/data/cache/ca-bundle.pem` (written by `core.net.ca_bundle_path()`). Angular 21 is used because Angular 22 needs Node 22+, and this machine has Node 20.19.

## Events, macro and the market view (Phase 3)

| Path | What it is |
|---|---|
| `ingestion/adapters/macro.py` | Brent, USD/INR, gold, US 10Y and dollar index via Yahoo's chart API. FRED timed out from this network, so it isn't used yet |
| `ingestion/adapters/nse_extra.py` | NSE FII/DII flows (latest day only, so history accumulates daily) and board meetings, which give the results dates |
| `ingestion/adapters/world_news.py` | Themed world news (GDELT, which returns 429 on this network, falling back to Google News RSS) and RBI/SEBI releases. Items naming private individuals are dropped and PAN numbers redacted (§11) |
| `ingestion/context.py` | Loaders plus `run_context_ingest`. News duplicates are clustered by title overlap (no embeddings without Postgres) |
| `ingestion/sensitivity_seed.py` | Initial factor → sector transmission map, approved but marked "seed: review me" |
| `quant/engine.py` | Event overlay factor (net sector impact × confidence, decayed). Regime now also uses FII flows, USD/INR, Brent and US yields |
| `orchestrator/map_review.py` | LLM-13 weekly map proposals, validated deterministically, then filed as review tasks |
| `orchestrator/premarket.py` | 07:30 pre-market brief (template, no LLM spend) |
| `web/src/app/pages/market.ts` | Market & Events screen: regime readings, macro tiles, flows, sector heat table, event feed, calendar, transmission map. Discovery lives on the Analyze page |

New schedule: 06:30 overnight ingest, 07:15 event digest, 07:30 pre-market brief, 22:45 flows and calendar, Sunday 10:00 map review.

```bash
stock-primer-data context        # macro, flows, results calendar, world news, RBI/SEBI (also seeds history: 400 days of macro)
python -m scheduler.main --once overnight|events|premarket|map
```

## Depth and trust (Phase 4)

| Path | What it is |
|---|---|
| `ingestion/adapters/nse_results.py` | NSE quarterly results: legacy listing (to Dec 2024) plus integrated filings (2025 on), XBRL parsed for companies and banks |
| `ingestion/fundamentals.py` | Prefers consolidated, then the latest revision. Deterministic checks: income sum; PAT vs PBT − tax (allowing associates, discontinued operations and utilities' regulatory deferrals below tax); EPS vs profit/shares; YoY plausibility. A misfiled owners' profit falls back to total profit. An EPS-only mismatch stores the quarter without EPS. Insurers' statement format isn't supported and is reported, not queued. Other failures become `data_anomaly` review tasks. Stored in ₹ crore with `published_at` = broadcast date |
| `ingestion/adapters/nse_announcements.py`, `ingestion/filing_pdf.py`, `ingestion/filing_text.py` | PDF fallback for quarters with no XBRL listed (mostly the Mar-2025 change-over, when NSE's listings dropped them).<br>1. Find the results PDF among the corporate announcements and download it (cached).<br>2. Extract text with pypdf and keep only the statement page(s) of the right type (consolidated/standalone).<br>3. Read them with LLM-15 (small model).<br>4. Run the XBRL checks, plus: confidence ≥ 0.8, core lines read, the right quarter and statement, and units within 3× of nearby quarters.<br>Doubtful extractions go to review. Scanned PDFs and "only the other statement is readable" cases are reported, not sent to the model. Run with `stock-primer-data fundamentals-pdf [--dry-run] [--limit N]`; the scheduler runs it on Saturdays at 13:00 (up to 20 quarters) |
| `quant/fundamentals.py`, `mcp_servers/tools_fundamentals.py` | Quality (margins, consistency, ROE), growth and earnings-yield factors (strategy `quant-v2`). `get_financials`, `get_ratios`, `get_valuation` and `get_shareholding` are point-in-time |
| `ingestion/adapters/nse_results.py` (`balance_sheet`) | Half-yearly balance sheet from the same XBRL (`OneI` context), in three formats: companies, Ind-AS lenders (NBFCs) and banks. Gives ROE (TTM owners' profit / average equity), ROCE and debt/equity and net debt (companies only), assets/equity (lenders), and P/B. Negative equity is flagged, not hidden. Ratios are compared only with peers of the same kind. `stock-primer-data balance-sheets` fills older rows from cached filings |
| Insurers (`nse_results.py`, `quant/fundamentals.insurer_metrics`) | Life (`IntegratedFinance_LI`) and general (`_GI`) insurer XBRL. Revenue = net premium (life) or net earned premium (general). PBT, tax and PAT come from the shareholders' account (life files policyholders' tax separately). No EBITDA or margins; instead gross-premium growth, new-business share (life), solvency (normalised when filed 100× too small), and combined/claims/expense ratios. The balance sheet gives ROE and P/B. Shareholders' funds are used only if they reconcile with capital + reserves (ICICI Lombard files them as −562 cr). No debt/equity, ROCE or assets/equity |
| `quant/fundamentals.ttm_eps` | Trailing EPS puts older quarters on the newest quarter's share basis (paid-up capital ÷ face value), so bonuses and splits don't inflate it (LIC's 1:1 bonus had halved its P/E) |
| `ingestion/adapters/bse.py` | BSE as a fallback for companies whose filings aren't on NSE (MCX files only with BSE). The scrip code is matched on ISIN. Results PDFs come from BSE announcements (same `ResultPdf` shape, so the PDF → OCR/images → LLM-15 → checks pipeline is reused through `ChainedResults`: NSE first, then BSE). Shareholding comes from BSE's summary statement (promoter / public / employee trusts) plus its public-shareholder table (FPI, DII, MF, insurance, government, retail). Categories are matched on label and parent category, cross-checked against the public total, and only totals are kept (the table names large holders). Rows are dated at the SEBI 21-day deadline (end of day) because BSE gives no timestamp |
| `ingestion/ocr.py` (+ `filing_pdf.find_scanned_statement`) | Scanned results PDFs. These are image-only pages, or pages with an unreadable embedded OCR layer.<br>1. Pages are rendered with pypdfium2 and OCR'd with RapidOCR (onnxruntime, pip-only, no Tesseract install).<br>2. Each page's heading band is screened first, and scanning stops at the first page titled as the wanted statement.<br>3. A full-page OCR confirms it.<br>4. The page image(s) go to LLM-15 with the OCR transcript as a guide; the model reads the values from the image.<br>Text-layer PDFs get page images too, because some text layers are the issuer's own poor OCR. OCR results are cached. Scans need extraction confidence ≥ 0.85 (text PDFs: 0.8) and are stored as `llm15_ocr_*`. CPU OCR takes seconds per page, so a scanned filing takes minutes. `--no-ocr` skips it |
| `ingestion/adapters/nse_shareholding.py`, `ingestion/shareholding.py` | Quarterly shareholding patterns (both XBRL formats) into `shareholding`: promoter, FPI, DII, MF, insurance, retail, government, and promoter encumbrance/pledge. Each row is dated by its broadcast, so a revision is only known from when it was filed. Checked: categories add to 100%, institutions within the float, XBRL vs the exchange headline. Only the totals are kept; the raw files list shareholders' names and PANs and are never stored. `stock-primer-data shareholding`; Sundays 11:00 in the scheduler |
| `orchestrator/shadow.py`, `simulator/engine.py` | Quant-only shadow portfolio: the screen's top picks with the same sizing and limits, with no agents or review |
| `evaluation/outcomes.py`, `evaluation/scorecard.py` | Rejected and expired picks are followed on paper as cohorts. Attribution covers sector, regime and each signal (raised vs lowered), plus agents-vs-quant value-add |
| `scheduler/jobs.py` | Event digest triggers drive shadow re-evaluation plus portfolio review (review tasks only). Weekly portfolio review. 21:30 results follow-up. Approved config is reloaded before each run |
| `orchestrator/llm.py` | Identical LLM requests (same prompt version and inputs) within `llm_cache_hours` reuse the stored answer, audited as `cached` |
| `api/main.py` | `POST /chat` (grounded Q&A job) and `POST /config/autonomy`, which creates an unapproved config version plus an `approve_config` task |
| `web/src/app/pages/ask.ts` and others | Ask page with evidence chips. Reco detail shows how the score was built and flags agent disagreement. Scorecard shows value-add, review cohorts and attribution. Settings has autonomy requests |


## Extras (Phase 5)

| Path | What it is |
|---|---|
| `simulator/stress.py` | What-if and stress tests (B6, D10). Shocks to the market, Brent, USD/INR, gold (%) or the US 10Y (pp). Sizes come from weekly regressions: the market is implied by the factors unless given; each sector is its market beta plus its beyond-market sensitivity; each stock is its shrunk beta plus its sector's. A link counts only at t ≥ 1.5, and each sector is checked against the approved map. Also a free-text parser ("crude hits $110 and the rupee falls 3%") and a model-free replay of the worst N-day window |
| `quant/compare.py` | Comparison of 2 to 5 stocks (A7): returns, volatility, drawdown, beta, 52-week position, quant score per horizon with failed gates, fundamentals, latest recommendation, and rebased series |
| `evaluation/export.py` | CSV export (D11): trades, NAV and positions per portfolio; recommendations with outcomes and cohorts |
| `simulator/alerts.py`, `ingestion/adapters/intraday.py` | Optional intraday alerts, off by default (`intraday_alerts` in config). Every 15 minutes, 09:15 to 15:30, delayed 5-minute Yahoo quotes are checked against stops and targets of open positions and live picks. Alerts are near stop, stop touched or target reached, at most once a day each (`alert_log`). Alerts only; fills stay end-of-day |
| `api/main.py` | `GET /stress/presets`, `POST /stress`, `POST /stress/replay`, `GET /compare`, `GET /export/...csv`, `POST /config/alerts`, `GET /alerts`. The kill switch and alert settings now start from the latest approved config, so they can't undo an approved autonomy change |
| `bot/main.py` | `/whatif <scenario or preset>` and `/compare <symbols>` |
| `web/src/app/pages/stress.ts`, `compare.ts` | What-if page (presets, free text, hand-built shocks, sector bars with map check, holdings and picks that would hit their stop, replay) and Compare page (rebased chart, side-by-side table). Export buttons on Portfolio and Recommendations; alert settings in Settings |

```bash
python -m scheduler.main --once alerts      # one intraday check (does nothing unless alerts are on and the market is open)
```

## Login and backups

See [deploy/README.md](deploy/README.md). Summary:

| Path | What it is |
|---|---|
| `deploy/Caddyfile`, `deploy/start.ps1`, `deploy/stop.ps1` | Caddy in front of everything: HTTPS, security headers (strict CSP: no inline scripts), and `forward_auth` to the API. The API, scheduler and bot bind to 127.0.0.1 |
| `api/auth.py` | Password (scrypt) + authenticator code (RFC 6238 TOTP, replay-blocked), lockout after 5 failures, HttpOnly/SameSite=Strict session cookie, login attempts logged as `security_event`. `python -m api.auth setup` enrols |
| `ops/backup.py` | Online SQLite backup, integrity check, gzip, AES-256-GCM (chunked, authenticated). Checksums kept in a manifest; optional second copy (encrypted only); 7/4/12 rotation; `verify` and `restore`. Scheduler: 02:30 nightly, restore test Sundays 03:30, Telegram alert on failure |

## Known limits and setup

Everything in the plan (phases 0-5, login, backups) is built. What remains is setup on this PC and known data limits.

Setup (see deploy/README.md):
- Install Caddy (`winget install CaddyServer.Caddy`) and run `python -m api.auth setup`.
- Put `STOCK_PRIMER_ANTHROPIC_API_KEY` and the Telegram token / owner chat id in `.env`.
- Copy `STOCK_PRIMER_BACKUP_KEY` to a password manager.
- Run `stock-primer-data fundamentals-pdf` once the API key is set (about 35 PDF-only quarters, MCX included).

Data limits:
- Insurer XBRL parses from 2025 on (5-6 quarters each); earlier insurer quarters aren't in that format.
- BSE-only filers' shareholding is dated at the SEBI deadline (BSE gives no broadcast time).
- OCR runs on CPU: a few minutes per scanned filing.
- SQLite for now; `STOCK_PRIMER_DATABASE_URL` can point at Postgres (backups would then use `pg_dump`).
