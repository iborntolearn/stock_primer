import { Component, computed, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { InrPipe } from '../core/format';
import { CompareItem, CompareResponse } from '../core/models';
import { LineChart, LineSeries } from '../shared/line-chart';

const MAX = 5;

/** A7: two to five stocks side by side. Each symbol keeps its colour slot while it stays in the list. */
@Component({
  selector: 'app-compare',
  imports: [FormsModule, RouterLink, InrPipe, LineChart],
  template: `
    <h1>Compare</h1>
    <form class="row card" (ngSubmit)="add()">
      <label class="grow">Add a symbol (2 to {{ MAX }})<input [(ngModel)]="entry" name="s" placeholder="INFY" [disabled]="symbols().length >= MAX" /></label>
      <button type="submit" [disabled]="!entry.trim() || symbols().length >= MAX">Add</button>
      <div class="row wrap">@for (s of symbols(); track s) {
        <span class="chip sym"><span class="sw" [style.background]="'var(--series-' + slots.get(s) + ')'"></span>{{ s }}
          <button type="button" class="x" (click)="remove(s)" [attr.aria-label]="'Remove ' + s">✕</button></span>
      }</div>
    </form>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    @if (symbols().length < 2) { <p class="muted">Add at least two symbols.</p> }
    @if (loading()) { <p class="skeleton">Loading…</p> }
    @if (data(); as d) {
      <section class="card gap">
        <h3>Price, rebased to 100 <span class="muted small">· as of {{ d.as_of }}</span></h3>
        <app-line-chart [series]="lines()" />
      </section>
      <section class="card table-wrap gap">
        <table class="cmp">
          <thead><tr><th></th>@for (x of d.items; track x.symbol) { <th class="num">{{ x.symbol }}</th> }</tr></thead>
          <tbody>
            <tr><td class="muted">Sector</td>@for (x of d.items; track x.symbol) { <td class="num small">{{ x.sector ?? '–' }}</td> }</tr>
            @for (r of priceRows; track r.key) {
              <tr><td class="muted">{{ r.label }}</td>@for (x of d.items; track x.symbol) {
                <td class="num" [class.best]="best(r.key, r.better) === x.symbol">{{ fmt(price(x, r.key), r.kind) }}</td> }</tr>
            }
            <tr class="sep"><td colspan="99">Quant score (percentile, gates)</td></tr>
            @for (h of horizons; track h) {
              <tr><td class="muted">{{ h }}</td>@for (x of d.items; track x.symbol) {
                <td class="num">@if (x.scores[h]; as q) { {{ q.quant_score | inr: 0 }}
                  @if (!q.gates_passed) { <span class="small muted" [title]="'Failed: ' + q.failed_gates.join(', ')">✗ {{ q.failed_gates[0] }}</span> } } @else { – }</td> }</tr>
            }
            <tr class="sep"><td colspan="99">Fundamentals (latest filed quarter and balance sheet)</td></tr>
            @for (r of fundRows; track r.key) {
              <tr><td class="muted">{{ r.label }}</td>@for (x of d.items; track x.symbol) { <td class="num">{{ fmt(x.fundamentals?.[r.key], r.kind) }}</td> }</tr>
            }
            @if (hasInsurer()) {
              <tr class="sep"><td colspan="99">Insurer metrics (latest filed quarter)</td></tr>
              @for (r of insRows; track r.key) {
                <tr><td class="muted">{{ r.label }}</td>@for (x of d.items; track x.symbol) {
                  <td class="num">{{ x.fundamentals?.['insurer'] ? fmt(x.fundamentals!['insurer'][r.key], r.kind) : 'n/a' }}</td> }</tr>
              }
            }
            <tr class="sep"><td colspan="99">Shareholding (latest filed quarter)</td></tr>
            @for (r of holdRows; track r.key) {
              <tr><td class="muted">{{ r.label }}</td>@for (x of d.items; track x.symbol) { <td class="num">{{ fmt(x.shareholding?.[r.key], r.kind) }}</td> }</tr>
            }
            <tr class="sep"><td colspan="99">Latest recommendation</td></tr>
            @for (h of horizons; track h) {
              <tr><td class="muted">{{ h }}</td>@for (x of d.items; track x.symbol) {
                <td class="num small">@if (x.recommendations[h]; as r) {
                  <a [routerLink]="['/recommendations', r.reco_id]">{{ r.verdict }}</a> <span class="muted">{{ r.status }} · {{ r.date }}</span>
                } @else { – }</td> }</tr>
            }
          </tbody>
        </table>
        <p class="small muted">Bold marks the better value where “better” is unambiguous. Fundamentals and balance sheets come from NSE results filings (balance sheets twice a year), shareholding from quarterly patterns; each is as published by the comparison date. For insurers, revenue is net (earned) premium; margins, ROCE and debt/equity don't apply.</p>
      </section>
    }`,
  styles: `
    .grow { flex: 1 1 200px; } .grow input { width: 100%; }
    .wrap { flex-wrap: wrap; gap: 6px; }
    .sym { display: inline-flex; align-items: center; gap: 6px; }
    .sw { width: 12px; height: 3px; border-radius: 2px; display: inline-block; }
    .x { border: 0; background: transparent; padding: 0 2px; cursor: pointer; color: var(--text-muted); }
    .gap { margin-top: 16px; }
    .cmp td.best { font-weight: 700; }
    tr.sep td { padding-top: 14px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-muted); }
  `,
})
export class Compare {
  symbolsParam = input<string>(undefined, { alias: 'symbols' });   // ?symbols=INFY,TCS
  private api = inject(Api);
  private router = inject(Router);
  readonly MAX = MAX;
  horizons = ['short', 'medium', 'long'];
  entry = '';
  symbols = signal<string[]>([]);
  slots = new Map<string, number>();       // colour follows the stock, not its position
  data = signal<CompareResponse | null>(null);
  loading = signal(false);
  error = signal('');

  priceRows: { key: string; label: string; kind: 'pct' | 'pctu' | 'num' | 'inr' | 'pos' | 'bool'; better?: 'high' | 'low' }[] = [
    { key: 'last', label: 'Last close', kind: 'inr' },
    { key: 'ret_1m_pct', label: '1-month return', kind: 'pct', better: 'high' },
    { key: 'ret_3m_pct', label: '3-month return', kind: 'pct', better: 'high' },
    { key: 'ret_6m_pct', label: '6-month return', kind: 'pct', better: 'high' },
    { key: 'ret_1y_pct', label: '1-year return', kind: 'pct', better: 'high' },
    { key: 'vol_ann_pct', label: 'Volatility (ann.)', kind: 'pctu', better: 'low' },
    { key: 'max_drawdown_1y_pct', label: 'Max drawdown (1y)', kind: 'pct', better: 'high' },
    { key: 'beta', label: 'Beta vs NIFTY 500', kind: 'num' },
    { key: 'range_52w_position', label: 'Position in 52w range', kind: 'pos' },
    { key: 'atr_pct', label: 'ATR % of price', kind: 'pctu' },
    { key: 'avg_traded_value_cr', label: 'Avg traded value (₹ cr)', kind: 'num' },
    { key: 'above_200dma', label: 'Above 200-day avg', kind: 'bool' },
  ];
  fundRows: { key: string; label: string; kind: 'pct' | 'pctu' | 'num' | 'inr' }[] = [
    { key: 'latest_quarter', label: 'Quarter', kind: 'num' },
    { key: 'revenue_yoy_pct', label: 'Revenue YoY', kind: 'pct' },
    { key: 'pat_yoy_pct', label: 'Profit YoY', kind: 'pct' },
    { key: 'op_margin_pct', label: 'Operating margin', kind: 'pct' },
    { key: 'pat_margin_pct', label: 'Net margin', kind: 'pct' },
    { key: 'pe', label: 'Trailing P/E', kind: 'num' },
    { key: 'pb', label: 'Price / book', kind: 'num' },
    { key: 'roe_pct', label: 'ROE (trailing 12m)', kind: 'pctu' },
    { key: 'roce_pct', label: 'ROCE (non-lenders)', kind: 'pctu' },
    { key: 'debt_equity', label: 'Debt / equity (non-lenders)', kind: 'num' },
    { key: 'balance_sheet_as_of', label: 'Balance sheet as of', kind: 'num' },
  ];
  // Insurers have no operating margin, ROCE or debt/equity; these are what matter instead.
  insRows: { key: string; label: string; kind: 'pct' | 'pctu' | 'num' }[] = [
    { key: 'type', label: 'Type', kind: 'num' },
    { key: 'gross_premium_cr', label: 'Gross premium (₹ cr, quarter)', kind: 'num' },
    { key: 'gross_premium_yoy_pct', label: 'Gross premium growth (YoY)', kind: 'pct' },
    { key: 'new_business_share_pct', label: 'New business share (life)', kind: 'pctu' },
    { key: 'solvency_ratio', label: 'Solvency ratio (min 1.5)', kind: 'num' },
    { key: 'combined_ratio_pct', label: 'Combined ratio (general)', kind: 'pctu' },
    { key: 'claims_ratio_pct', label: 'Claims ratio (general)', kind: 'pctu' },
    { key: 'expense_of_management_ratio_pct', label: 'Expense of management ratio', kind: 'pctu' },
  ];
  hasInsurer = computed(() => (this.data()?.items ?? []).some((x) => !!x.fundamentals?.['insurer']));
  holdRows: { key: string; label: string; kind: 'pct' | 'pctu' | 'num' }[] = [
    { key: 'period_end', label: 'Quarter', kind: 'num' },
    { key: 'promoter_pct', label: 'Promoter', kind: 'pctu' },
    { key: 'promoter_change_1y', label: 'Promoter, 1y change (pp)', kind: 'num' },
    { key: 'promoter_encumbered_pct', label: 'Promoter shares encumbered', kind: 'pctu' },
    { key: 'fpi_pct', label: 'Foreign institutions', kind: 'pctu' },
    { key: 'fpi_change_1y', label: 'Foreign, 1y change (pp)', kind: 'num' },
    { key: 'dii_pct', label: 'Domestic institutions', kind: 'pctu' },
    { key: 'mf_pct', label: 'of which mutual funds', kind: 'pctu' },
    { key: 'retail_pct', label: 'Retail (resident individuals)', kind: 'pctu' },
  ];

  lines = computed<LineSeries[]>(() => {
    const d = this.data();
    if (!d) return [];
    const out: LineSeries[] = d.items.map((x) => ({ key: x.symbol, label: x.symbol, slot: this.slots.get(x.symbol) ?? null,
                                                    points: d.rebased[x.symbol] ?? [] }));
    if (d.rebased[d.benchmark]) out.push({ key: d.benchmark, label: d.benchmark, slot: null, points: d.rebased[d.benchmark] });
    return out;
  });

  ngOnInit(): void {
    for (const s of (this.symbolsParam() ?? '').split(',')) this.push(s);
    this.load();
  }

  private push(raw: string): boolean {
    const s = raw.trim().toUpperCase();
    if (!s || this.symbols().includes(s) || this.symbols().length >= MAX) return false;
    const used = new Set(this.slots.values());
    this.slots.set(s, [1, 2, 3, 4, 5].find((n) => !used.has(n))!);
    this.symbols.update((x) => [...x, s]);
    return true;
  }

  add(): void { if (this.push(this.entry)) { this.entry = ''; this.sync(); } }
  remove(s: string): void { this.symbols.update((x) => x.filter((y) => y !== s)); this.slots.delete(s); this.sync(); }

  private sync(): void {
    this.router.navigate([], { queryParams: { symbols: this.symbols().join(',') || null }, replaceUrl: true });
    this.load();
  }

  private load(): void {
    if (this.symbols().length < 2) { this.data.set(null); return; }
    this.loading.set(true); this.error.set('');
    this.api.compare(this.symbols()).subscribe({
      next: (d) => { this.data.set(d); this.loading.set(false); },
      error: (e) => { this.error.set(errorText(e)); this.loading.set(false); },
    });
  }

  price(x: CompareItem, key: string): unknown { return (x.prices as Record<string, unknown> | null)?.[key]; }

  best(key: string, better?: 'high' | 'low'): string | null {
    const items = this.data()?.items ?? [];
    if (!better || items.length < 2) return null;
    const vals = items.map((x) => ({ s: x.symbol, v: (x.prices as any)?.[key] as number | null })).filter((x) => x.v !== null && x.v !== undefined);
    if (vals.length < 2) return null;
    vals.sort((a, b) => (better === 'high' ? b.v! - a.v! : a.v! - b.v!));
    return vals[0].v === vals[1].v ? null : vals[0].s;
  }

  fmt(v: unknown, kind: string): string {
    if (v === null || v === undefined) return '–';
    if (kind === 'bool') return v ? 'yes' : 'no';
    if (typeof v === 'string') return v;
    const n = Number(v);
    if (kind === 'pct') return `${n > 0 ? '+' : ''}${n.toFixed(1)}%`;
    if (kind === 'pctu') return `${n.toFixed(1)}%`;
    if (kind === 'pos') return `${Math.round(n * 100)}%`;
    if (kind === 'inr') return '₹' + n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
    return n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
  }
}
export type { CompareItem };
