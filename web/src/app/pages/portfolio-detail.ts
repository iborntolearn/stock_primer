import { Component, inject, input, signal } from '@angular/core';
import { toObservable } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { switchMap } from 'rxjs';

import { Api, errorText } from '../core/api.service';
import { InrPipe, IstPipe, PctPipe } from '../core/format';
import { Order, PortfolioDetail as Detail, Trade } from '../core/models';
import { NavChart } from '../shared/nav-chart';

/** S7: NAV vs benchmark, metrics, holdings, trades and orders. */
@Component({
  selector: 'app-portfolio-detail',
  imports: [RouterLink, InrPipe, IstPipe, PctPipe, NavChart],
  template: `
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    @if (d(); as p) {
      <div class="row spread head">
        <h1>{{ p.name }} <span class="chip">{{ typeLabel(p.type) }}</span></h1>
        <div class="row">
          <a [routerLink]="['/stress']" [queryParams]="{ portfolio: p.portfolio_id }">Stress test</a>
          <button class="ghost" (click)="csv(p.portfolio_id, p.name, 'trades')">Trades CSV</button>
          <button class="ghost" (click)="csv(p.portfolio_id, p.name, 'nav')">NAV CSV</button>
          <button class="ghost" (click)="csv(p.portfolio_id, p.name, 'positions')">Positions CSV</button>
        </div>
      </div>
      @if (p.metrics; as m) {
        <div class="tiles">
          <div class="card"><h3>NAV</h3><p class="big num">₹{{ m.nav | inr: 0 }}</p></div>
          <div class="card"><h3>Return</h3><p class="big num">{{ m.total_return_pct | pct }}</p>
            <p class="small muted">{{ p.benchmark }} {{ m.benchmark_return_pct | pct }}</p></div>
          <div class="card"><h3>Alpha</h3><p class="big num">{{ m.alpha_pct | pct }}</p></div>
          <div class="card"><h3>Max drawdown</h3><p class="big num">{{ m.max_drawdown_pct | pct: 1 : false }}</p></div>
          <div class="card"><h3>Hit rate</h3><p class="big num">{{ m.hit_rate_pct === null ? '–' : (m.hit_rate_pct | pct: 0 : false) }}</p>
            <p class="small muted">{{ m.closed_trades }} closed · costs ₹{{ m.costs_paid | inr: 0 }}</p></div>
        </div>
      }
      <section class="card"><app-nav-chart [points]="p.nav_series" [label]="p.name" [benchLabel]="p.benchmark" /></section>

      <div class="tabs" role="tablist">
        @for (t of tabs; track t) { <button role="tab" [class.on]="tab() === t" [attr.aria-selected]="tab() === t" (click)="tab.set(t)">{{ t }}</button> }
      </div>
      <section class="card table-wrap">
        @switch (tab()) {
          @case ('Holdings') {
            <table><thead><tr><th>Symbol</th><th>Horizon</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">Last</th>
              <th class="num">P&amp;L</th><th class="num">Stop</th><th class="num">Target</th><th>Expires</th><th>Card</th></tr></thead>
              <tbody>@for (h of p.positions; track h.symbol + h.opened_on) {
                <tr><td><strong>{{ h.symbol }}</strong></td><td>{{ h.horizon }}</td><td class="num">{{ h.qty }}</td>
                  <td class="num">{{ h.avg_cost | inr }}</td><td class="num">{{ h.last | inr }}</td>
                  <td class="num" [class.up]="h.pnl_pct > 0" [class.down]="h.pnl_pct < 0">{{ h.pnl_pct | pct }}</td>
                  <td class="num">{{ h.stop | inr }}</td><td class="num">{{ h.target | inr }}</td><td>{{ h.expires_on | ist: true }}</td>
                  <td>@if (h.reco_id) { <a [routerLink]="['/recommendations', h.reco_id]">#{{ h.reco_id }}</a> }</td></tr>
              } @empty { <tr><td colspan="10" class="muted">No open positions. Cash ₹{{ p.cash | inr: 0 }}.</td></tr> }</tbody></table>
          }
          @case ('Trades') {
            <table><thead><tr><th>Date</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Costs</th><th>Reason</th></tr></thead>
              <tbody>@for (t of trades(); track t.id) {
                <tr><td>{{ t.date | ist: true }}</td><td>{{ t.symbol }}</td><td>{{ t.side }}</td><td class="num">{{ t.qty }}</td>
                  <td class="num">{{ t.price | inr }}</td><td class="num">{{ t.costs | inr }}</td><td>{{ t.reason }}</td></tr>
              } @empty { <tr><td colspan="7" class="muted">No trades yet.</td></tr> }</tbody></table>
          }
          @case ('Orders') {
            <table><thead><tr><th>Placed</th><th>Symbol</th><th>Status</th><th class="num">Amount / qty</th><th class="num">Stop</th><th class="num">Target</th><th>Note</th></tr></thead>
              <tbody>@for (o of orders(); track o.id) {
                <tr><td>{{ o.placed_at | ist }}</td><td>{{ o.symbol }}</td><td><span class="chip">{{ o.status }}</span></td>
                  <td class="num">{{ o.amount !== null ? '₹' + (o.amount | inr: 0) : o.qty }}</td><td class="num">{{ o.stop | inr }}</td>
                  <td class="num">{{ o.target | inr }}</td><td class="small secondary">{{ o.reject_reason }}</td></tr>
              } @empty { <tr><td colspan="7" class="muted">No orders.</td></tr> }</tbody></table>
          }
          @case ('Rules') {
            <dl class="rules">@for (k of ruleKeys(p.rules); track k) { <dt>{{ k }}</dt><dd>{{ fmtRule(p.rules[k]) }}</dd> }</dl>
          }
        }
      </section>
    } @else if (!error()) { <p class="skeleton">Loading…</p> }`,
  styles: `
    .head h1 { margin: 0; } .head { margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
    h1 .chip { vertical-align: middle; }
    .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .big { font-size: 20px; margin: 0; }
    .tabs { display: flex; gap: 4px; margin: 16px 0 8px; }
    .tabs button.on { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
    .up { color: var(--good-text); } .down { color: var(--critical-text); }
    .rules { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0; }
    .rules dt { color: var(--text-muted); } .rules dd { margin: 0; }
  `,
})
export class PortfolioDetail {
  id = input.required<string>();
  private api = inject(Api);
  tabs = ['Holdings', 'Trades', 'Orders', 'Rules'];
  tab = signal('Holdings');
  d = signal<Detail | null>(null);
  trades = signal<Trade[]>([]);
  orders = signal<Order[]>([]);
  error = signal('');

  typeLabel(t: string): string {
    return ({ auto_follow: 'Auto-follow', quant_shadow: 'Quant-only shadow', manual: 'Manual' } as Record<string, string>)[t] ?? t;
  }

  csv(pid: number, name: string, kind: string): void {
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || `portfolio-${pid}`;
    this.api.download(`/export/portfolios/${pid}/${kind}.csv`, `${slug}-${kind}.csv`).subscribe({ error: (e) => this.error.set(errorText(e)) });
  }

  constructor() {
    toObservable(this.id).pipe(switchMap((id) => this.api.portfolio(Number(id)))).subscribe({
      next: (p) => {
        this.d.set(p);
        this.api.trades(p.portfolio_id).subscribe((t) => this.trades.set(t.items));
        this.api.orders(p.portfolio_id).subscribe((o) => this.orders.set(o.items));
      },
      error: (e) => this.error.set(errorText(e)),
    });
  }

  ruleKeys(r: Record<string, unknown>): string[] { return Object.keys(r ?? {}).sort(); }
  fmtRule(v: unknown): string { return typeof v === 'object' ? JSON.stringify(v) : String(v); }
}
