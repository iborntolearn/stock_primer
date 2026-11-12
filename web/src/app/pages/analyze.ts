import { Component, DestroyRef, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { AnalysisResult, JobEvent } from '../core/models';
import { AddToPortfolioState } from '../shared/add-to-portfolio';
import { CardView, RecoCard, viewFromCard } from '../shared/reco-card';

/** S4 (analyze part): on-demand single-stock analysis with streamed progress. Never auto-published. */
@Component({
  selector: 'app-analyze',
  imports: [FormsModule, RouterLink, RecoCard],
  template: `
    <div class="row spread"><h1>{{ mode() === 'stock' ? 'Analyze a stock' : 'Discover ideas' }}</h1>
      <div class="seg" role="tablist">
        <button role="tab" [class.on]="mode() === 'stock'" (click)="mode.set('stock')">Analyze a stock</button>
        <button role="tab" [class.on]="mode() === 'discover'" (click)="mode.set('discover')">Discover ideas</button>
      </div></div>
    <form class="row card" (ngSubmit)="start()">
      @if (mode() === 'stock') {
        <label class="grow">Symbol or company name<input [(ngModel)]="query" name="q" placeholder="HDFCBANK" /></label>
      } @else {
        <label class="grow">Sector (optional)<select [(ngModel)]="sector" name="sector">
          <option [ngValue]="null">Any sector</option>
          @for (x of sectors(); track x) { <option [ngValue]="x">{{ x }}</option> }</select></label>
        <label>Ideas<select [(ngModel)]="maxIdeas" name="n">@for (n of [3, 5, 8]; track n) { <option [ngValue]="n">{{ n }}</option> }</select></label>
      }
      @for (h of all; track h) {
        <label class="check"><input type="checkbox" [checked]="horizons.has(h)" (change)="toggle(h)" [attr.aria-label]="h + ' horizon'" /> {{ h }}</label>
      }
      <button class="primary" type="submit" [disabled]="running() || (mode() === 'stock' && !query.trim()) || !horizons.size">
        {{ running() ? 'Working…' : mode() === 'stock' ? 'Analyze' : 'Find ideas' }}</button>
    </form>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    @if (events().length) {
      <section class="card progress" aria-live="polite">
        <h3>Progress</h3>
        <ol>@for (e of events(); track e.seq) { <li [class.done]="e.kind === 'done'">{{ e.message }}</li> }</ol>
      </section>
    }
    @if (clarification()) { <p class="banner warn">{{ clarification() }}</p> }
    @for (e of resultErrors(); track e) { <p class="banner warn small">{{ e }}</p> }
    <div class="grid grid-2 results">
      @for (r of results(); track r.symbol + r.horizon) {
        @if (r.card) {
          <app-reco-card [v]="view(r)" (addToSim)="add.open($event)" />
        } @else {
          <article class="card"><strong>{{ r.symbol }}</strong> <span class="chip">{{ r.horizon }}</span> <span class="chip">{{ r.status }}</span>
            <p class="secondary small">{{ reason(r) }}</p>
            @if (r.reco_id) { <a [routerLink]="['/recommendations', r.reco_id]">Details</a> }</article>
        }
      }
    </div>
    <p class="muted small">{{ mode() === 'discover' ? 'Discovery screens the universe with the current regime, then researches the top names. ' : '' }}
      On-demand results are stored but never published or added to the auto-follow portfolio.</p>`,
  styles: `
    form { align-items: end; }
    .grow { flex: 1 1 220px; }
    .grow input { width: 100%; }
    .check { display: flex; flex-direction: row; align-items: center; gap: 4px; font-size: 14px; color: var(--text-primary); }
    .progress, .results, .banner { margin-top: 16px; }
    ol { margin: 0; padding-left: 20px; }
    li.done { font-weight: 600; }
    .seg { display: flex; gap: 4px; }
    .seg button.on { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
  `,
})
export class Analyze {
  symbol = input<string>();           // ?symbol= query param
  private api = inject(Api);
  add = inject(AddToPortfolioState);
  private destroyRef = inject(DestroyRef);
  all = ['short', 'medium', 'long'];
  horizons = new Set(this.all);
  query = '';
  mode = signal<'stock' | 'discover'>('stock');
  sector: string | null = null;
  maxIdeas = 5;
  sectors = signal<string[]>([]);
  running = signal(false);
  events = signal<JobEvent[]>([]);
  results = signal<AnalysisResult[]>([]);
  resultErrors = signal<string[]>([]);
  clarification = signal('');
  error = signal('');
  private stop: (() => void) | null = null;

  ngOnInit(): void {
    const s = this.symbol();
    if (s) { this.query = s; this.start(); }
  }

  constructor() {
    this.destroyRef.onDestroy(() => this.stop?.());
    this.api.sectors().subscribe({ next: (r) => this.sectors.set(r.sectors.map((x) => x.sector).sort()), error: () => {} });
  }

  toggle(h: string): void { this.horizons.has(h) ? this.horizons.delete(h) : this.horizons.add(h); }

  view(r: AnalysisResult): CardView { return viewFromCard(r.card!, r.reco_id, r.status); }

  reason(r: AnalysisResult): string {
    return r.violations.map((v) => v.message).slice(0, 2).join('; ') || 'No card was produced.';
  }

  start(): void {
    this.stop?.();
    this.running.set(true);
    this.events.set([]); this.results.set([]); this.resultErrors.set([]); this.clarification.set(''); this.error.set('');
    const horizons = this.all.filter((h) => this.horizons.has(h));
    const call = this.mode() === 'stock' ? this.api.analyze(this.query.trim(), horizons)
                                         : this.api.discover(horizons, this.sector, this.maxIdeas);
    call.subscribe({
      next: ({ job_id }) => this.follow(job_id),
      error: (e) => { this.error.set(errorText(e)); this.running.set(false); },
    });
  }

  private follow(id: number): void {
    const url = this.api.jobEventsUrl(id);
    if (url && typeof EventSource !== 'undefined') {
      const es = new EventSource(url);
      this.stop = () => es.close();
      const onEvent = (m: MessageEvent) => this.events.update((x) => [...x, JSON.parse(m.data)]);
      for (const k of ['started', 'progress', 'done', 'failed']) es.addEventListener(k, onEvent as EventListener);
      es.addEventListener('result', (m: MessageEvent) => { es.close(); this.finish(JSON.parse(m.data)); });
      es.onerror = () => { es.close(); this.poll(id); };    // fall back to polling
    } else {
      this.poll(id);
    }
  }

  private poll(id: number): void {
    const timer = setInterval(() => {
      this.api.job(id).subscribe({
        next: (j) => {
          this.events.set(j.events);
          if (j.status !== 'running') { clearInterval(timer); this.finish(j); }
        },
        error: (e) => { clearInterval(timer); this.error.set(errorText(e)); this.running.set(false); },
      });
    }, 1000);
    this.stop = () => clearInterval(timer);
  }

  private finish(j: { status: string; error: string | null; result: any; events?: JobEvent[] }): void {
    if (j.events) this.events.set(j.events);
    this.running.set(false);
    if (j.status === 'failed') { this.error.set(j.error ?? 'Analysis failed'); return; }
    this.clarification.set(j.result?.clarification ?? '');
    this.results.set(j.result?.results ?? []);
    this.resultErrors.set(j.result?.errors ?? []);
  }
}
