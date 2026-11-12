import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { InrPipe, IstPipe, PctPipe } from '../core/format';
import { CalendarItem, MacroResponse, MapRow, MarketEvent, Regime, SectorRow } from '../core/models';
import { Sparkline } from '../shared/sparkline';

const MACRO = [
  { id: 'BRENT', label: 'Brent', unit: '$' }, { id: 'USDINR', label: 'USD/INR', unit: '₹' },
  { id: 'US10Y', label: 'US 10Y yield', unit: '' }, { id: 'GOLD', label: 'Gold', unit: '$' },
  { id: 'DXY', label: 'Dollar index', unit: '' },
];
const FACTOR_LABEL: Record<string, string> = {
  crude_up: 'Crude rising', usdinr_up: 'Rupee weakening', us10y_up: 'US yields rising', gold_up: 'Gold rising',
  repo_up: 'RBI repo rising', monsoon_weak: 'Weak monsoon',
};

/** S5: regime, macro and flows, sector returns with the event overlay, event feed, calendar, transmission map. */
@Component({
  selector: 'app-market',
  imports: [RouterLink, InrPipe, PctPipe, IstPipe, Sparkline],
  template: `
    <h1>Market &amp; events</h1>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }

    <div class="grid grid-2">
      <section class="card">
        <h3>Regime</h3>
        @if (regime(); as r) {
          <p class="label"><strong>{{ r.regime.replace('_', '-').toUpperCase() }}</strong>
            <span class="muted small"> score {{ r.score }} · {{ r.as_of | ist: true }}</span></p>
          <dl class="readings small">
            <dt>Nifty 500 trend</dt><dd>{{ r.inputs['nifty500_trend'] ?? '–' }}</dd>
            <dt>Breadth (above 200-DMA)</dt><dd>{{ r.inputs['breadth_pct_above_200dma'] ?? '–' }}%</dd>
            <dt>India VIX</dt><dd>{{ r.inputs['india_vix'] ?? '–' }} <span class="muted">({{ r.inputs['india_vix_chg_5d_pct'] | pct }} 5d)</span></dd>
            <dt>FII net, 5 days</dt><dd>{{ crore(r.inputs['fii_5d_cr']) }}</dd>
            <dt>USD/INR, 5 days</dt><dd>{{ r.inputs['usdinr_chg_5d_pct'] | pct }}</dd>
            <dt>Brent, 5 days</dt><dd>{{ r.inputs['brent_chg_5d_pct'] | pct }}</dd>
            <dt>US 10Y</dt><dd>{{ r.inputs['us10y'] ?? '–' }}%</dd>
          </dl>
          @if (r.inputs['unavailable']?.length) { <p class="muted small">Not yet available: {{ r.inputs['unavailable'].join(', ') }}</p> }
          @if (r.assessment.narrative) { <p>{{ r.assessment.narrative }}</p> }
        } @else { <p class="muted">No regime snapshot yet.</p> }
      </section>

      <section class="card">
        <h3>Macro</h3>
        <div class="tiles">
          @for (x of macroTiles(); track x.id) {
            <div class="tile">
              <div class="muted small">{{ x.label }}</div>
              <div class="num val">{{ x.last === null ? '–' : x.unit + (x.last | inr: 2) }}</div>
              <div class="small num">{{ fmtChg(x.chg_1d, x.isPp) }} <span class="muted">1d</span> · {{ fmtChg(x.chg_5d, x.isPp) }} <span class="muted">5d</span></div>
              <app-sparkline [values]="x.values" />
            </div>
          }
        </div>
        <h3 class="flows-h">FII / DII cash flows (₹ cr)</h3>
        @if (macro()?.flows?.length) {
          <table class="small"><thead><tr><th>Date</th><th class="num">FII</th><th class="num">DII</th></tr></thead>
            <tbody>@for (f of flowsDesc(); track f.date) {
              <tr><td>{{ f.date | ist: true }}</td>
                <td class="num" [class.neg]="(f.fii_net_cr ?? 0) < 0">{{ f.fii_net_cr | inr: 0 }}</td>
                <td class="num" [class.neg]="(f.dii_net_cr ?? 0) < 0">{{ f.dii_net_cr | inr: 0 }}</td></tr>
            }</tbody></table>
          <p class="muted small">History starts when ingestion began; NSE publishes only the latest day.</p>
        } @else { <p class="muted small">No flows ingested yet.</p> }
      </section>
    </div>

    <section class="card">
      <div class="row spread"><h3>Sectors</h3><span class="muted small">as of {{ sectorsAsOf() | ist: true }}</span></div>
      <p class="muted small">Returns from NSE sector indices. Event overlay = net impact of active events (direction × magnitude × confidence, fading over time):
        <span class="swatch pos"></span> tailwind, <span class="swatch neg"></span> headwind.</p>
      <div class="table-wrap">
        <table class="heat">
          <thead><tr><th>Sector</th><th class="num">5 days</th><th class="num">1 month</th><th class="num">3 months</th><th class="num">Event overlay</th></tr></thead>
          <tbody>@for (r of sectorsSorted(); track r.sector) {
            <tr><td>{{ r.sector }}</td>
              <td class="num" [style.background]="cell(r.ret_5d_pct, 5)">{{ r.ret_5d_pct | pct: 1 }}</td>
              <td class="num" [style.background]="cell(r.ret_1m_pct, 10)">{{ r.ret_1m_pct | pct: 1 }}</td>
              <td class="num" [style.background]="cell(r.ret_3m_pct, 20)">{{ r.ret_3m_pct | pct: 1 }}</td>
              <td class="num" [style.background]="cell(r.event_overlay, 0.6)">{{ r.event_overlay === 0 ? '–' : (r.event_overlay > 0 ? '+' : '') + r.event_overlay.toFixed(2) }}</td></tr>
          } @empty { <tr><td colspan="5" class="muted">No sector index history yet.</td></tr> }</tbody>
        </table>
      </div>
    </section>

    <div class="grid grid-2">
      <section class="card">
        <h3>Active events</h3>
        @for (e of events(); track e.id) {
          <article class="event">
            <div class="row"><span class="chip sev" [attr.data-sev]="e.severity">sev {{ e.severity }}</span>
              <strong>{{ e.title }}</strong>
              @if (e.status === 'unverified') { <span class="chip warn small">unverified</span> }</div>
            @if (e.summary) { <p class="small secondary">{{ e.summary }}</p> }
            <div class="row impacts">
              @for (i of e.impacts; track $index) {
                <span class="chip small" [class.good]="i.direction > 0" [class.bad]="i.direction < 0"
                      [title]="(i.basis === 'llm_inference' ? 'LLM inference (capped, reviewed)' : 'Approved map') + (i.channel ? ' · ' + i.channel : '')">
                  {{ i.direction > 0 ? '▲' : '▼' }} {{ i.sector }} {{ i.magnitude.toFixed(1) }}{{ i.basis === 'llm_inference' ? '*' : '' }}</span>
              }
            </div>
          </article>
        } @empty { <p class="muted">No active events. The 07:15 event digest extracts them from overnight news.</p> }
        @if (hasInferred()) { <p class="muted small">* inferred by the LLM where the approved map has no row; capped and reviewed.</p> }
      </section>

      <section class="card">
        <h3>Calendar, next 30 days</h3>
        <ul class="cal">
          @for (c of calendar(); track $index) {
            <li [class.pick]="c.open_pick">
              <span class="num date">{{ c.date | ist: true }}</span>
              @if (c.kind === 'holiday') { <span class="chip">holiday</span> {{ c.detail }} }
              @else { <strong>{{ c.symbol }}</strong> <span class="chip small">{{ c.kind }}</span>
                @if (c.open_pick) { <span class="chip small good">open pick</span> } }
            </li>
          } @empty { <li class="muted">Nothing scheduled.</li> }
        </ul>
      </section>
    </div>

    <section class="card">
      <div class="row spread"><h3>Transmission map</h3>
        @if (proposedCount()) { <a routerLink="/inbox">{{ proposedCount() }} proposed change{{ proposedCount() === 1 ? '' : 's' }} to review</a> }</div>
      <p class="muted small">How macro factors move sectors. Event impacts start from these rows. Seed rows are marked for your review.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Factor</th><th>Sector</th><th>Effect</th><th class="num">Magnitude</th><th>Why</th><th>Status</th></tr></thead>
        <tbody>@for (r of mapRows(); track r.id) {
          <tr [class.proposed]="r.status === 'proposed'">
            <td>{{ factorLabel(r.factor) }}</td><td>{{ r.sector }}</td>
            <td><span class="chip small" [class.good]="r.direction > 0" [class.bad]="r.direction < 0">{{ r.direction > 0 ? '▲ helps' : '▼ hurts' }}</span></td>
            <td class="num">{{ r.magnitude.toFixed(2) }}</td><td class="small secondary">{{ r.rationale }}</td>
            <td><span class="chip small">{{ r.status }}</span> @if (r.needs_review) { <span class="chip small warn">review</span> }</td></tr>
        } @empty { <tr><td colspan="6" class="muted">No map rows yet; run <code>stock-primer-data seed</code>.</td></tr> }</tbody>
      </table></div>
    </section>`,
  styles: `
    section.card, .grid { margin-bottom: 16px; }
    .label { margin: 0 0 8px; font-size: 18px; }
    .readings { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 0 0 8px; }
    .readings dt { color: var(--text-muted); } .readings dd { margin: 0; font-variant-numeric: tabular-nums; }
    .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; }
    .tile { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
    .val { font-size: 16px; font-weight: 600; }
    .flows-h { margin-top: 16px; }
    .neg { color: var(--critical-text); }
    .heat td.num { min-width: 84px; }
    .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: middle; }
    .swatch.pos { background: color-mix(in oklab, var(--div-pos) 55%, var(--div-mid)); }
    .swatch.neg { background: color-mix(in oklab, var(--div-neg) 55%, var(--div-mid)); }
    .event + .event { border-top: 1px solid var(--grid); padding-top: 10px; margin-top: 10px; }
    .event p { margin: 4px 0; }
    .impacts { gap: 4px; }
    .sev[data-sev="4"], .sev[data-sev="5"] { border-color: var(--critical); }
    .sev[data-sev="3"] { border-color: var(--warning); }
    .cal { list-style: none; margin: 0; padding: 0; max-height: 360px; overflow: auto; }
    .cal li { padding: 4px 0; border-bottom: 1px solid var(--grid); display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .cal li.pick { font-weight: 600; }
    .cal .date { width: 92px; color: var(--text-secondary); }
    tr.proposed td { background: var(--surface-2); }
  `,
})
export class Market {
  private api = inject(Api);
  regime = signal<Regime | null>(null);
  macro = signal<MacroResponse | null>(null);
  sectors = signal<SectorRow[]>([]);
  sectorsAsOf = signal<string | null>(null);
  events = signal<MarketEvent[]>([]);
  calendar = signal<CalendarItem[]>([]);
  mapRows = signal<MapRow[]>([]);
  error = signal('');

  macroTiles = computed(() => MACRO.map((m) => {
    const s = this.macro()?.series[m.id];
    return { ...m, last: s?.last ?? null, chg_1d: s?.chg_1d ?? null, chg_5d: s?.chg_5d ?? null, isPp: s?.unit === 'pp',
             values: (s?.points ?? []).slice(-30).map((p) => p.value) };
  }));
  flowsDesc = computed(() => [...(this.macro()?.flows ?? [])].reverse().slice(0, 10));
  // Sectors with neither index history nor an event impact carry no information; leave them out.
  sectorsSorted = computed(() => this.sectors().filter((r) => r.event_overlay !== 0 || r.ret_1m_pct !== null || r.ret_5d_pct !== null).sort((a, b) => Math.abs(b.event_overlay) - Math.abs(a.event_overlay)
    || (b.ret_1m_pct ?? -999) - (a.ret_1m_pct ?? -999)));
  hasInferred = computed(() => this.events().some((e) => e.impacts.some((i) => i.basis === 'llm_inference')));
  proposedCount = computed(() => this.mapRows().filter((r) => r.status === 'proposed').length);

  constructor() {
    const fail = (e: unknown) => this.error.set(errorText(e));
    this.api.regime().subscribe({ next: (r) => this.regime.set(r), error: () => {} });
    this.api.macro().subscribe({ next: (m) => this.macro.set(m), error: fail });
    this.api.sectors().subscribe({ next: (r) => { this.sectors.set(r.sectors); this.sectorsAsOf.set(r.as_of); }, error: fail });
    this.api.events(14).subscribe({ next: (r) => this.events.set([...r.events].sort((a, b) => b.severity - a.severity)), error: fail });
    this.api.calendar(30).subscribe({ next: (r) => this.calendar.set(r.items), error: fail });
    this.api.sensitivity().subscribe({ next: (r) => this.mapRows.set(r.rows), error: fail });
  }

  factorLabel(f: string): string { return FACTOR_LABEL[f] ?? f; }

  crore(v: number | null | undefined): string {
    if (v === null || v === undefined) return '–';
    return `${v < 0 ? '-' : '+'}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })} cr`;
  }

  fmtChg(v: number | null, pp: boolean): string {
    if (v === null) return '–';
    return `${v > 0 ? '+' : ''}${v.toFixed(2)}${pp ? 'pp' : '%'}`;
  }

  /** Diverging fill: blue for positive, red for negative, neutral gray at zero; the value is always printed. */
  cell(v: number | null, max: number): string {
    if (v === null || v === 0) return 'transparent';
    const t = Math.min(Math.abs(v) / max, 1) * 55;
    return `color-mix(in oklab, ${v > 0 ? 'var(--div-pos)' : 'var(--div-neg)'} ${t.toFixed(0)}%, var(--div-mid))`;
  }
}
