import { UpperCasePipe } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { InrPipe, IstPipe, PctPipe } from '../core/format';
import { Digest, Horizon, PortfolioSummary, PremarketBrief, RecoRow, Regime } from '../core/models';
import { AddToPortfolioState } from '../shared/add-to-portfolio';
import { CardView, RecoCard } from '../shared/reco-card';

/** S1 Home / daily digest: regime, what needs attention, open picks by horizon, simulated portfolios. */
@Component({
  selector: 'app-home',
  imports: [RouterLink, RecoCard, InrPipe, PctPipe, IstPipe, UpperCasePipe],
  template: `
    <h1>Today</h1>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    <div class="grid grid-2">
      <section class="card">
        <h3>Market regime</h3>
        @if (regime(); as r) {
          <p class="regime"><strong>{{ r.regime.replace('_', '-') | uppercase }}</strong>
            <span class="muted small"> score {{ r.score }} · as of {{ r.as_of | ist: true }}</span></p>
          <p class="secondary small readings">
            Nifty 500 trend {{ r.inputs['nifty500_trend'] ?? '–' }} ·
            breadth {{ r.inputs['breadth_pct_above_200dma'] ?? '–' }}% above 200-DMA ·
            India VIX {{ r.inputs['india_vix'] ?? '–' }}
          </p>
          @if (r.assessment.narrative) { <p>{{ r.assessment.narrative }}</p> }
        } @else { <p class="muted">No regime snapshot yet. It is computed by the first daily run or analysis.</p> }
      </section>
      <section class="card">
        <h3>Needs your attention</h3>
        <ul class="plain">
          @if (pending() > 0) { <li><a routerLink="/inbox">{{ pending() }} item{{ pending() === 1 ? '' : 's' }} awaiting review</a></li> }
          @for (a of digest()?.attention ?? []; track a) { @if (!a.startsWith('Awaiting review')) { <li>{{ a }}</li> } }
          @if (pending() === 0 && !(digest()?.attention?.length)) { <li class="muted">Nothing to review.</li> }
        </ul>
        @if (digest(); as d) {
          <p class="muted small">Last digest {{ d.date | ist: true }}</p>
          @for (n of d.no_pick_notes; track n) { <p class="small secondary">No picks — {{ n }}</p> }
        }
      </section>
    </div>

    @if (brief(); as b) {
      <section class="card brief">
        <div class="row spread"><h3>Pre-market brief · {{ b.date | ist: true }}</h3><a routerLink="/market">Market &amp; events</a></div>
        <div class="row macro">
          @for (x of b.macro; track x.series) {
            <span class="num"><span class="muted">{{ x.label }}</span> {{ x.last | inr }} <span [class.neg]="x.change < 0">({{ x.change > 0 ? '+' : '' }}{{ x.change.toFixed(2) }}{{ x.unit }})</span></span>
          }
          @if (b.flows; as f) { <span class="num"><span class="muted">FII</span> {{ f.fii_net_cr | inr: 0 }} cr · <span class="muted">DII</span> {{ f.dii_net_cr | inr: 0 }} cr</span> }
        </div>
        @for (e of b.events; track e.event_id) { <p class="small"><span class="chip small">sev {{ e.severity }}</span> {{ e.title }}</p> }
        @for (a of b.attention; track a) { <p class="small">⚠ {{ a }}</p> }
      </section>
    }

    <section class="picks">
      <div class="row spread">
        <h2>Open picks</h2>
        <div class="tabs" role="tablist">
          @for (h of horizons; track h) {
            <button role="tab" [class.on]="tab() === h" [attr.aria-selected]="tab() === h" (click)="tab.set(h)">
              {{ h }} ({{ byHorizon()[h].length }})</button>
          }
        </div>
      </div>
      @if (loading()) { <p class="skeleton">Loading…</p> }
      @else if (byHorizon()[tab()].length === 0) {
        <p class="muted">No open {{ tab() }}-term picks. A day with no picks is a valid outcome when the regime or evidence is weak.</p>
      } @else {
        <div class="grid grid-2">
          @for (c of byHorizon()[tab()]; track c.recoId) { <app-reco-card [v]="c" (addToSim)="add.open($event)" /> }
        </div>
      }
    </section>

    <section class="card">
      <div class="row spread"><h3>Simulated portfolios</h3><a routerLink="/portfolios">Open</a></div>
      @for (p of portfolios(); track p.id) {
        <p class="row spread"><span>{{ p.name }} <span class="chip">{{ p.type.replace('_', '-') }}</span></span>
          @if (p.metrics; as m) {
            <span class="num">₹{{ m.nav | inr: 0 }} · {{ m.total_return_pct | pct }}
              @if (m.benchmark_return_pct !== null) { <span class="muted">vs Nifty 500 {{ m.benchmark_return_pct | pct }}</span> }</span>
          } @else { <span class="muted">no NAV yet</span> }
        </p>
      } @empty { <p class="muted">No portfolios yet.</p> }
    </section>`,
  styles: `
    section + section, .grid + section { margin-top: 16px; }
    .regime { margin: 0 0 4px; font-size: 18px; }
    .readings { margin: 0 0 8px; }
    ul.plain { margin: 0; padding-left: 18px; }
    .tabs { display: flex; gap: 4px; }
    .tabs button { text-transform: capitalize; }
    .tabs button.on { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
    .picks h2 { margin: 0; }
    .picks .grid { margin-top: 12px; }
    .brief { margin-top: 16px; }
    .brief p { margin: 4px 0; }
    .macro { gap: 16px; margin-bottom: 8px; }
    .neg { color: var(--critical-text); }
  `,
})
export class Home {
  private api = inject(Api);
  add = inject(AddToPortfolioState);
  horizons: Horizon[] = ['short', 'medium', 'long'];
  tab = signal<Horizon>('short');
  regime = signal<Regime | null>(null);
  digest = signal<Digest | null>(null);
  recos = signal<RecoRow[]>([]);
  portfolios = signal<PortfolioSummary[]>([]);
  pending = signal(0);
  brief = signal<PremarketBrief | null>(null);
  loading = signal(true);
  error = signal('');

  byHorizon = computed(() => {
    const out: Record<Horizon, CardView[]> = { short: [], medium: [], long: [] };
    for (const r of this.recos()) {
      out[r.horizon].push({ recoId: r.id, symbol: r.symbol, horizon: r.horizon, verdict: r.verdict, status: r.status,
        confidence: r.confidence, headline: r.headline,
        levels: { entry_low: r.entry_low, entry_high: r.entry_high, stop: r.stop, target: r.target } });
    }
    return out;
  });

  constructor() {
    const fail = (e: unknown) => this.error.set(errorText(e));
    this.api.regime().subscribe({ next: (r) => this.regime.set(r), error: () => {} });
    this.api.premarket().subscribe({ next: (b) => this.brief.set(b), error: () => {} });
    this.api.digest().subscribe({ next: (d) => this.digest.set(d), error: () => {} });
    this.api.tasks().subscribe({ next: (t) => this.pending.set(t.items.length), error: fail });
    this.api.portfolios().subscribe({ next: (p) => this.portfolios.set(p.items), error: () => {} });
    this.api.recommendations({ status: 'published,active', limit: 60 }).subscribe({
      next: (r) => { this.recos.set(r.items); this.loading.set(false); },
      error: (e) => { fail(e); this.loading.set(false); },
    });
  }
}
