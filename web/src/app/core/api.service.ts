import { HttpClient, HttpHeaders, HttpParams } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Observable } from 'rxjs';

import {
  AlertRow, CalendarItem, ChatJob, CompareResponse, Digest, ReplayResult, Shock, StressPresets, StressResult, Evidence, Health, HitlTask, Job, MacroResponse, MapRow, MarketEvent, Order, OrderResult,
  PortfolioDetail, PortfolioSummary, PremarketBrief, RecoDetail, RecoRow, Regime, RunDetail, RunRow, Scorecard, SectorRow, Trade,
} from './models';

const TOKEN_KEY = 'stock_primer.api_token';

function readToken(): string {
  try { return localStorage.getItem(TOKEN_KEY) ?? ''; } catch { return ''; }
}

/** Thin client for the FastAPI backend. The dev server proxies /api to http://127.0.0.1:8000. */
@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);
  readonly token = signal(readToken());

  setToken(t: string): void {
    this.token.set(t);
    try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
  }

  private headers(extra: Record<string, string> = {}): HttpHeaders {
    const t = this.token();
    return new HttpHeaders({ ...(t ? { Authorization: `Bearer ${t}` } : {}), ...extra });
  }

  private get<T>(path: string, params: Record<string, string | number | undefined | null> = {}): Observable<T> {
    let p = new HttpParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') p = p.set(k, String(v));
    return this.http.get<T>(`/api/v1${path}`, { headers: this.headers(), params: p });
  }

  private post<T>(path: string, body: unknown, extra: Record<string, string> = {}): Observable<T> {
    return this.http.post<T>(`/api/v1${path}`, body, { headers: this.headers(extra) });
  }

  health() { return this.get<Health>('/health'); }
  digest(date?: string) { return this.get<Digest>('/digest', { date }); }
  regime() { return this.get<Regime>('/market/regime'); }
  recommendations(q: { horizon?: string; status?: string; symbol?: string; limit?: number } = {}) {
    return this.get<{ items: RecoRow[] }>('/recommendations', q);
  }
  recommendation(id: number) { return this.get<RecoDetail>(`/recommendations/${id}`); }
  evidence(id: string) { return this.get<Evidence>(`/evidence/${id}`); }

  analyze(symbol: string, horizons: string[]) { return this.post<{ job_id: number }>('/analyze', { symbol, horizons }); }
  discover(horizons: string[], sector: string | null, maxIdeas: number) {
    return this.post<{ job_id: number }>('/discover', { horizons, sector, max_ideas: maxIdeas });
  }
  macro(days = 60) { return this.get<MacroResponse>('/market/macro', { days }); }
  sectors() { return this.get<{ as_of: string | null; sectors: SectorRow[] }>('/market/sectors'); }
  calendar(days = 30, scope = 'watch') { return this.get<{ from: string; to: string; items: CalendarItem[] }>('/market/calendar', { days, scope }); }
  sensitivity() { return this.get<{ rows: MapRow[] }>('/market/sensitivity'); }
  events(days = 14) { return this.get<{ events: MarketEvent[] }>('/market/events', { days }); }
  premarket() { return this.get<PremarketBrief>('/premarket'); }
  job(id: number) { return this.get<Job>(`/jobs/${id}`); }
  chat(message: string, history: string[]) { return this.post<{ job_id: number }>('/chat', { message, history }); }
  chatJob(id: number) { return this.get<ChatJob>(`/jobs/${id}`); }
  /** Server-sent events for a job; EventSource cannot send headers, so this falls back to polling when a token is set. */
  jobEventsUrl(id: number): string | null { return this.token() ? null : `/api/v1/jobs/${id}/events`; }

  portfolios() { return this.get<{ items: PortfolioSummary[] }>('/portfolios'); }
  createPortfolio(body: { name: string; starting_cash: number; type: string }) { return this.post<{ id: number }>('/portfolios', body); }
  portfolio(id: number) { return this.get<PortfolioDetail>(`/portfolios/${id}`); }
  trades(id: number) { return this.get<{ items: Trade[] }>(`/portfolios/${id}/trades`); }
  orders(id: number) { return this.get<{ items: Order[] }>(`/portfolios/${id}/orders`); }
  placeOrder(pid: number, body: Record<string, unknown>, idempotencyKey: string) {
    return this.post<OrderResult>(`/portfolios/${pid}/orders`, body, { 'Idempotency-Key': idempotencyKey });
  }

  tasks(status = 'pending') { return this.get<{ items: HitlTask[] }>('/hitl/tasks', { status }); }
  decide(id: number, action: string, note?: string, edits: Record<string, unknown> = {}) {
    return this.post<{ status: string; effects: string[] }>(`/hitl/tasks/${id}/decision`, { action, note, edits });
  }

  scorecard() { return this.get<Scorecard>('/scorecard'); }
  runs(limit = 50) { return this.get<{ items: RunRow[] }>('/runs', { limit }); }
  run(id: number) { return this.get<RunDetail>(`/runs/${id}`); }
  config() { return this.get<Record<string, any>>('/config'); }
  requestAutonomy(action: string, level: number, note: string) {
    return this.post<{ config_version: number; task_id: number; status: string }>('/config/autonomy', { action, level, note });
  }
  setAlerts(enabled: boolean, stopProximityPct: number) {
    return this.post<{ intraday_alerts: boolean; stop_proximity_pct: number }>('/config/alerts', { enabled, stop_proximity_pct: stopProximityPct });
  }
  alerts(days = 7) { return this.get<{ items: AlertRow[] }>('/alerts', { days }); }

  stressPresets() { return this.get<StressPresets>('/stress/presets'); }
  stress(body: { preset?: string; text?: string; shocks?: Shock[]; portfolio_ids?: number[] }) { return this.post<StressResult>('/stress', body); }
  replay(body: { days?: number; start?: string; end?: string; portfolio_ids?: number[] }) { return this.post<ReplayResult>('/stress/replay', body); }
  compare(symbols: string[], days = 250) { return this.get<CompareResponse>('/compare', { symbols: symbols.join(','), days }); }

  /** CSV download. Fetched with the auth header, then saved through a temporary object URL. */
  download(path: string, filename: string): Observable<void> {
    return new Observable<void>((sub) => {
      const req = this.http.get(`/api/v1${path}`, { headers: this.headers(), responseType: 'blob' }).subscribe({
        next: (blob) => {
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url; a.download = filename; a.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          sub.next(); sub.complete();
        },
        error: (e) => sub.error(e),
      });
      return () => req.unsubscribe();
    });
  }

  killSwitch(enabled: boolean) { return this.post<{ kill_switch: boolean }>('/admin/kill-switch', { enabled }); }
}

/** Pull a readable message out of an API error ({code, message} envelope or a network failure). */
export function errorText(e: any): string {
  if (e?.error?.message) return e.error.message;
  if (e?.status === 0) return 'Cannot reach the API. Is it running on port 8000?';
  if (e?.status === 401) return 'Unauthorised: set the API token in Settings.';
  return e?.message ?? 'Something went wrong';
}
