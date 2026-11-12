import { Component, computed, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { InrPipe, PctPipe } from '../core/format';
import { PortfolioSummary, ReplayResult, Shock, StressPresets, StressResult } from '../core/models';
import { DivBar } from '../shared/div-bar';

/** B6 / D10: what-if scenarios and stress tests on the simulated portfolios and open picks. Deterministic, no LLM. */
@Component({
  selector: 'app-stress',
  imports: [FormsModule, RouterLink, InrPipe, PctPipe, DivBar],
  template: `
    <h1>What-if &amp; stress test</h1>
    <p class="secondary small">Estimates how a shock to the market, crude, the rupee, US yields or gold would move sectors, your simulated
      portfolios and open picks, from weekly history up to {{ presets()?.as_of ?? '…' }}. Estimates, not forecasts.</p>

    <section class="card stack">
      <form class="row" (ngSubmit)="runText()">
        <label class="grow">Scenario
          <input [(ngModel)]="text" name="t" maxlength="500" placeholder="e.g. crude hits $110 and the rupee falls 3%" /></label>
        <button class="primary" type="submit" [disabled]="busy() || !text.trim()">Run</button>
      </form>
      <div class="row wrap">
        <span class="muted small">Presets:</span>
        @for (p of presets()?.presets ?? []; track p.id) {
          <button type="button" class="chip-btn" (click)="runPreset(p.id)" [disabled]="busy()">{{ p.name }}</button>
        }
      </div>
      <details>
        <summary>Build shocks by hand</summary>
        @for (s of custom; track $index) {
          <div class="row">
            <select [(ngModel)]="s.factor" [name]="'f' + $index" aria-label="Factor">
              @for (f of presets()?.factors ?? []; track f.code) { <option [value]="f.code">{{ f.label }} ({{ f.unit }})</option> }
            </select>
            <input type="number" step="0.1" [(ngModel)]="s.change" [name]="'c' + $index" aria-label="Change" class="num narrow" />
            <span class="muted small">{{ unit(s.factor) }} · now {{ latest(s.factor) }}</span>
            <button type="button" class="ghost" (click)="custom.splice($index, 1)" aria-label="Remove shock">✕</button>
          </div>
        }
        <div class="row"><button type="button" class="ghost" (click)="custom.push({ factor: 'BRENT', change: 10 })">+ Add shock</button>
          <button type="button" (click)="runCustom()" [disabled]="busy() || !custom.length">Run these shocks</button></div>
      </details>
      @if (portfolios().length) {
        <div class="row wrap"><span class="muted small">Portfolios:</span>
          @for (p of portfolios(); track p.id) {
            <label class="check"><input type="checkbox" [checked]="chosen.has(p.id)" (change)="togglePortfolio(p.id)" /> {{ p.name }}</label>
          }</div>
      }
    </section>

    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    @if (busy()) { <p class="skeleton">Estimating…</p> }

    @if (result(); as r) {
      <h2>{{ r.scenario.name }}</h2>
      <p class="small secondary">@for (s of r.scenario.shocks; track s.factor) { <span class="chip">{{ label(s.factor) }} {{ s.change > 0 ? '+' : '' }}{{ s.change }}{{ unit(s.factor) }}</span> }</p>
      @for (n of r.notes; track n) { <p class="banner warn small">{{ n }}</p> }
      <div class="tiles">
        <div class="card"><h3>Market (NIFTY 500)</h3><p class="big num" [class.down]="r.market.move_pct < 0" [class.up]="r.market.move_pct > 0">{{ r.market.move_pct | pct }}</p>
          <p class="small muted">{{ r.market.basis }}</p></div>
        @for (p of r.portfolios; track p.portfolio_id) {
          <div class="card"><h3>{{ p.name }}</h3>
            <p class="big num" [class.down]="p.pnl < 0" [class.up]="p.pnl > 0">₹{{ p.pnl | inr: 0 }}</p>
            <p class="small muted">{{ p.pnl_pct | pct }} of NAV · {{ p.holdings.length }} holdings
              @if (p.stops_breached) { · <span class="bad-text">{{ p.stops_breached }} stop{{ p.stops_breached > 1 ? 's' : '' }} hit</span> }</p></div>
        }
      </div>

      <div class="grid grid-2 gap">
        <section class="card table-wrap">
          <h3>Sectors</h3>
          <table>
            <thead><tr><th>Sector</th><th class="num">Est. move</th><th class="barcol"><span class="sr-only">Direction</span></th><th>Map check</th></tr></thead>
            <tbody>@for (x of r.sectors; track x.sector) {
              <tr [title]="why(x)"><td>{{ x.sector }}</td><td class="num">{{ x.move_pct | pct }}</td>
                <td class="barcol"><app-div-bar [value]="x.move_pct" [max]="maxSector()" [label]="x.sector + ' ' + x.move_pct + '%'" /></td>
                <td class="small">@for (c of x.map; track c.factor) {
                  <span class="chip" [class.good]="c.agreement === 'agrees'" [class.bad]="c.agreement === 'disagrees'"
                        [title]="c.rationale ?? ''">{{ c.factor }}: map {{ c.map_direction > 0 ? '▲' : '▼' }} · {{ c.agreement }}</span>
                }</td></tr>
            } @empty { <tr><td colspan="4" class="muted">No sector index has enough history.</td></tr> }</tbody>
          </table>
          <p class="small muted">Hover a row for the arithmetic. “Map check” compares history with the approved transmission map.</p>
        </section>
        <section class="card table-wrap">
          <h3>Open picks</h3>
          <table><thead><tr><th>Pick</th><th class="num">Est. move</th><th class="num">Est. price</th><th class="num">Stop</th><th></th></tr></thead>
            <tbody>@for (p of r.open_picks; track p.reco_id) {
              <tr><td><a [routerLink]="['/recommendations', p.reco_id]">{{ p.symbol }}</a> <span class="muted small">{{ p.horizon }}</span></td>
                <td class="num">{{ p.move_pct | pct }}</td><td class="num">{{ p.est_price | inr }}</td><td class="num">{{ p.stop | inr }}</td>
                <td>@if (p.breaches_stop) { <span class="chip bad">⛔ stop</span> }</td></tr>
            } @empty { <tr><td colspan="5" class="muted">No open favourable picks.</td></tr> }</tbody></table>
        </section>
      </div>

      @for (p of r.portfolios; track p.portfolio_id) {
        @if (p.holdings.length) {
          <section class="card table-wrap gap">
            <h3>{{ p.name }} holdings</h3>
            <table><thead><tr><th>Symbol</th><th class="num">Weight</th><th class="num">Est. move</th><th class="num">Est. P&amp;L</th>
              <th class="num">Est. price</th><th class="num">Stop</th><th>Basis</th><th></th></tr></thead>
              <tbody>@for (h of p.holdings; track h.symbol) {
                <tr><td><strong>{{ h.symbol }}</strong> <span class="muted small">{{ h.sector }}</span></td>
                  <td class="num">{{ h.weight_pct === null || h.weight_pct === undefined ? '–' : (h.weight_pct | pct: 1 : false) }}</td>
                  <td class="num">{{ h.move_pct | pct }}</td><td class="num">₹{{ h.pnl | inr: 0 }}</td>
                  <td class="num">{{ h.est_price | inr }}</td><td class="num">{{ h.stop | inr }}</td>
                  <td class="small muted">{{ h.basis }}</td>
                  <td>@if (h.breaches_stop) { <span class="chip bad">⛔ stop</span> } @if (h.reaches_target) { <span class="chip good">🎯 target</span> }</td></tr>
              }</tbody></table>
          </section>
        }
      }
      <details class="gap"><summary class="small">Assumptions</summary>
        <ul class="small secondary">@for (a of r.assumptions; track a) { <li>{{ a }}</li> }</ul></details>
    }

    <section class="card stack gap">
      <h2>Historical replay</h2>
      <p class="small secondary">Today's holdings moved by their real returns over the market's worst window in stored history. No model involved.</p>
      <div class="row">
        <label>Window<select [(ngModel)]="replayDays" name="rd">@for (d of [5, 10, 20, 60]; track d) { <option [ngValue]="d">{{ d }} trading days</option> }</select></label>
        <button (click)="runReplay()" [disabled]="busy()">Replay worst window</button>
      </div>
      @if (replay(); as rp) {
        <p><strong>{{ rp.window.start }} → {{ rp.window.end }}</strong>: NIFTY 500 {{ rp.window.benchmark_pct | pct }}</p>
        <div class="tiles">@for (p of rp.portfolios; track p.portfolio_id) {
          <div class="card"><h3>{{ p.name }}</h3><p class="big num" [class.down]="p.pnl < 0">₹{{ p.pnl | inr: 0 }}</p>
            <p class="small muted">{{ p.pnl_pct | pct }} of NAV @if (p.stops_breached) { · {{ p.stops_breached }} below stop }</p></div>
        }</div>
        <table class="small"><thead><tr><th>Sector</th><th class="num">Move</th><th class="barcol"></th></tr></thead>
          <tbody>@for (x of rp.sectors; track x.sector) {
            <tr><td>{{ x.sector }}</td><td class="num">{{ x.move_pct | pct }}</td>
              <td class="barcol"><app-div-bar [value]="x.move_pct" [max]="maxReplay()" /></td></tr>
          }</tbody></table>
      }
    </section>
    <p class="muted small">Decision-support only. Not investment advice.</p>`,
  styles: `
    .grow { flex: 1 1 280px; } .grow input { width: 100%; }
    .wrap { flex-wrap: wrap; gap: 6px; }
    .chip-btn { border-radius: 999px; padding: 4px 12px; font-size: 13px; }
    .narrow { width: 90px; }
    .check { display: inline-flex; flex-direction: row; align-items: center; gap: 4px; font-size: 14px; color: var(--text-primary); }
    .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 12px 0; }
    .tiles h3 { margin: 0 0 4px; }
    .big { font-size: 22px; font-weight: 600; margin: 0; }
    .up { color: var(--good-text); } .down, .bad-text { color: var(--critical-text); }
    .barcol { width: 30%; min-width: 90px; }
    .gap { margin-top: 16px; }
    h2 { margin: 20px 0 4px; }
    details summary { cursor: pointer; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    .banner { margin: 8px 0; }
  `,
})
export class Stress {
  portfolio = input<string>();             // ?portfolio= to preselect one
  private api = inject(Api);
  presets = signal<StressPresets | null>(null);
  portfolios = signal<PortfolioSummary[]>([]);
  chosen = new Set<number>();
  text = '';
  custom: Shock[] = [{ factor: 'BRENT', change: 20 }];
  replayDays = 20;
  busy = signal(false);
  error = signal('');
  result = signal<StressResult | null>(null);
  replay = signal<ReplayResult | null>(null);
  maxSector = computed(() => Math.max(1, ...(this.result()?.sectors ?? []).map((x) => Math.abs(x.move_pct))));
  maxReplay = computed(() => Math.max(1, ...(this.replay()?.sectors ?? []).map((x) => Math.abs(x.move_pct))));

  constructor() {
    this.api.stressPresets().subscribe({ next: (p) => this.presets.set(p), error: (e) => this.error.set(errorText(e)) });
    this.api.portfolios().subscribe({
      next: (r) => {
        this.portfolios.set(r.items);
        const want = Number(this.portfolio());
        for (const p of r.items) if (!want || p.id === want) this.chosen.add(p.id);
      },
      error: () => {},
    });
  }

  togglePortfolio(id: number): void { this.chosen.has(id) ? this.chosen.delete(id) : this.chosen.add(id); }
  label(code: string): string { return this.presets()?.factors.find((f) => f.code === code)?.label ?? code; }
  unit(code: string): string { return this.presets()?.factors.find((f) => f.code === code)?.unit ?? '%'; }
  latest(code: string): string { const v = this.presets()?.latest?.[code]; return v === undefined ? '–' : v.toLocaleString('en-IN', { maximumFractionDigits: 2 }); }

  why(x: StressResult['sectors'][number]): string {
    return x.parts.map((p) => `${p.factor}: ${p.per_unit} per unit (t ${p.t})${p.used === false ? ' not significant, left out' : ''} → ${p.points > 0 ? '+' : ''}${p.points}%`)
      .join('\n') + `\n${x.weeks} weeks of history`;
  }

  private ids(): number[] { return [...this.chosen]; }

  runText(): void { this.run({ text: this.text.trim() }); }
  runPreset(id: string): void { this.run({ preset: id }); }
  runCustom(): void { this.run({ shocks: this.custom.map((s) => ({ factor: s.factor, change: Number(s.change) })) }); }

  private run(body: { preset?: string; text?: string; shocks?: Shock[] }): void {
    this.busy.set(true); this.error.set('');
    this.api.stress({ ...body, portfolio_ids: this.ids() }).subscribe({
      next: (r) => { this.result.set(r); this.busy.set(false); },
      error: (e) => { this.error.set(errorText(e)); this.busy.set(false); },
    });
  }

  runReplay(): void {
    this.busy.set(true); this.error.set('');
    this.api.replay({ days: this.replayDays, portfolio_ids: this.ids() }).subscribe({
      next: (r) => { this.replay.set(r); this.busy.set(false); },
      error: (e) => { this.error.set(errorText(e)); this.busy.set(false); },
    });
  }
}
