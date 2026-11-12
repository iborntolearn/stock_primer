import { Component, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { toObservable } from '@angular/core/rxjs-interop';
import { switchMap } from 'rxjs';

import { Api, errorText } from '../core/api.service';
import { InrPipe, IstPipe, PctPipe } from '../core/format';
import { RecoDetail as Detail } from '../core/models';
import { AddToPortfolioState } from '../shared/add-to-portfolio';
import { ConfidenceMeter } from '../shared/confidence-meter';
import { EvidenceChip } from '../shared/evidence';
import { viewFromCard } from '../shared/reco-card';

/** S3: verdict, levels and confidence up top; thesis, cases, risks, critic and evidence below. */
@Component({
  selector: 'app-reco-detail',
  imports: [RouterLink, InrPipe, IstPipe, PctPipe, ConfidenceMeter, EvidenceChip],
  template: `
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    @if (d(); as r) {
      <div class="row spread head">
        <h1>{{ r.symbol }} · {{ r.horizon }}-term</h1>
        <div class="row">
          <a [routerLink]="['/analyze']" [queryParams]="{ symbol: r.symbol }">Analyze again</a>
          <a [routerLink]="['/compare']" [queryParams]="{ symbols: r.symbol }">Compare</a>
          @if (r.verdict === 'favourable') { <button class="primary" (click)="addToSim()">Add to portfolio</button> }
        </div>
      </div>
      <section class="card stack">
        <div class="row">
          <span class="chip" [class.good]="r.verdict === 'favourable'" [class.bad]="r.verdict === 'avoid'">{{ r.verdict }}</span>
          <span class="chip">{{ r.status }}</span>
          <app-confidence [value]="r.confidence" [why]="r.card.confidence_rationale" />
          <span class="muted small">score {{ r.final_score | inr: 1 }} (quant {{ r.quant_score | inr: 1 }}{{ adj() }})</span>
        </div>
        @if (r.card.headline) { <p class="headline">{{ r.card.headline }}</p> }
        <div class="levels num">
          <span><span class="muted">Entry</span> {{ r.entry_low | inr }}–{{ r.entry_high | inr }}</span>
          <span><span class="muted">Stop</span> {{ r.stop | inr }}</span>
          <span><span class="muted">Target</span> {{ r.target | inr }}</span>
          <span><span class="muted">R:R</span> {{ r.risk_reward | inr }}</span>
        </div>
        @if (r.card.human_edits?.length) { <p class="small secondary">Edited in review: {{ r.card.human_edits!.join('; ') }}</p> }
        @if (r.outcome; as o) {
          <p class="banner">Outcome: {{ o.ret_pct | pct }} (alpha {{ o.alpha_pct | pct }}) over {{ o.days_held }} days
            · {{ o.hit_target ? 'target hit' : o.hit_stop ? 'stopped out' : r.close_reason }}</p>
        }
        <p>{{ r.card.thesis }}</p>
      </section>

      @if (r.contributions.length) {
        <section class="card build">
          <div class="row spread"><h3>How the score was built</h3>
            @if (disagree()) { <span class="chip warn" title="Signals pulled in opposite directions">⚠ Agents disagree</span> }</div>
          <p class="small muted">Quant {{ r.quant_score | inr: 1 }} decides; agents may nudge it by at most the configured cap, and only with cited evidence.</p>
          <table class="contrib">
            <thead><tr><th>Signal</th><th>View</th><th class="num">Points</th><th class="barcol"><span class="sr-only">Direction</span></th></tr></thead>
            <tbody>
              @for (c of r.contributions; track c.source) {
                <tr>
                  <td>{{ c.source }}</td>
                  <td class="small secondary">{{ stance(c.source) }}</td>
                  <td class="num">{{ c.points > 0 ? '+' : '' }}{{ c.points | inr: 1 }}</td>
                  <td class="barcol"><div class="bar" [attr.aria-label]="c.points + ' points'">
                    <span class="mid"></span>
                    @if (c.points !== 0) {
                      <span class="fill" [class.neg]="c.points < 0" [style.width.%]="width(c.points)"></span>
                    }</div></td>
                </tr>
              }
              <tr class="total"><td>Final</td><td class="small secondary">capped sum{{ capped() ? ' (cap applied)' : '' }}</td>
                <td class="num">{{ r.final_score | inr: 1 }}</td><td></td></tr>
            </tbody>
          </table>
        </section>
      }

      <div class="grid grid-2 sections">
        <section class="card">
          <h3>Bull case</h3>
          <ul>@for (b of r.card.bull_case; track $index) { <li>{{ b }}</li> }</ul>
          <h3>Bear case</h3>
          <ul>@for (b of r.card.bear_case; track $index) { <li>{{ b }}</li> }</ul>
        </section>
        <section class="card">
          <h3>Key risks</h3>
          <ul>@for (k of r.card.key_risks; track $index) {
            <li>{{ k.risk }} @for (e of k.evidence_ids; track e) { <app-evidence-chip [id]="e" /> }</li>
          }</ul>
          <h3>Invalidation</h3>
          <ul>@for (i of r.card.invalidation; track $index) {
            <li>{{ i.condition }} <span class="muted small">{{ i.machine_checkable ? '(auto-checked)' : '(review)' }}</span></li>
          }</ul>
          @if (r.card.what_to_watch.length) {
            <h3>What to watch</h3><ul>@for (w of r.card.what_to_watch; track $index) { <li>{{ w }}</li> }</ul>
          }
        </section>
        <section class="card">
          <h3>Claims and evidence</h3>
          <ul>@for (c of r.card.claims; track $index) {
            <li>{{ c.text }} @for (e of c.evidence_ids; track e) { <app-evidence-chip [id]="e" /> }</li>
          }</ul>
        </section>
        <section class="card">
          <h3>Critic</h3>
          @if (r.critique; as c) {
            <p><span class="chip">{{ c.verdict }}</span> <span class="muted small">{{ c.base_rate_note }}</span></p>
            <ul>@for (a of c.strongest_counterarguments; track $index) {
              <li><span class="chip" [class.bad]="a.severity === 'high'">{{ a.severity }}</span> {{ a.argument }}</li>
            }</ul>
          } @else { <p class="muted">No critic report for this card.</p> }
          @if (r.violations.length) {
            <h3>Validation</h3>
            <ul class="small">@for (v of r.violations; track $index) { <li><code>{{ v.code }}</code> {{ v.action }} — {{ v.message }}</li> }</ul>
          }
          @if (r.gaps.length) { <h3>Data gaps</h3><p class="small secondary">{{ r.gaps.join(' · ') }}</p> }
        </section>
      </div>

      <section class="card meta small muted">
        Evidence ({{ r.evidence.length }}): @for (e of r.evidence; track e.id) { <app-evidence-chip [id]="e.id" /> }
        @if (r.run; as run) {
          <p>Run <a [routerLink]="['/runs', run.id]">#{{ run.id }}</a> · {{ run.run_type }} · decided {{ run.decision_time | ist }}
            · config v{{ run.config_version }} · prompts {{ run.prompt_bundle_version }} · strategy {{ run.strategy_version }}</p>
        }
      </section>
    } @else if (!error()) { <p class="skeleton">Loading…</p> }`,
  styles: `
    .head h1 { margin: 0; }
    .head { margin-bottom: 16px; }
    .headline { font-size: 16px; margin: 0; }
    .levels { display: flex; gap: 4px 20px; flex-wrap: wrap; font-size: 15px; }
    .levels > span { white-space: nowrap; }
    .sections { margin: 16px 0; }
    ul { margin: 0 0 12px; padding-left: 18px; }
    li + li { margin-top: 4px; }
    .meta app-evidence-chip { margin-right: 2px; }
    .build { margin-top: 16px; }
    .build h3 { margin: 0; }
    .contrib { width: 100%; }
    .barcol { width: 40%; }
    .bar { position: relative; height: 10px; }
    .bar .mid { position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: var(--axis); }
    .bar .fill { position: absolute; left: 50%; height: 100%; background: var(--div-pos); border-radius: 0 4px 4px 0; }
    .bar .fill.neg { left: auto; right: 50%; background: var(--div-neg); border-radius: 4px 0 0 4px; }
    tr.total td { font-weight: 600; border-top: 1px solid var(--grid); }
    .chip.warn { background: var(--warning); color: #0b0b0b; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  `,
})
export class RecoDetail {
  id = input.required<string>();      // route param (withComponentInputBinding)
  private api = inject(Api);
  private add = inject(AddToPortfolioState);
  d = signal<Detail | null>(null);
  error = signal('');
  adj = computed(() => {
    const r = this.d();
    if (!r) return '';
    const a = r.final_score - r.quant_score;
    return a === 0 ? '' : `, agents ${a > 0 ? '+' : ''}${a.toFixed(1)}`;
  });

  /** Signals that moved the score in opposite directions by a meaningful amount. */
  disagree = computed(() => {
    const c = this.d()?.contributions ?? [];
    return c.some((x) => x.points >= 1) && c.some((x) => x.points <= -1);
  });
  capped = computed(() => {
    const r = this.d();
    if (!r) return false;
    const raw = r.contributions.reduce((a, c) => a + c.points, 0);
    return Math.abs(raw - (r.final_score - r.quant_score)) > 0.05;
  });
  private maxAbs = computed(() => Math.max(1, ...(this.d()?.contributions ?? []).map((c) => Math.abs(c.points))));

  width(points: number): number { return (Math.abs(points) / this.maxAbs()) * 50; }

  stance(source: string): string {
    const a = this.d()?.agents?.[source];
    if (!a) return '';
    switch (source) {
      case 'technical': return `${a.stance} · ${a.trend} trend · ${a.setup}`;
      case 'fundamental': return `${a.earnings_trajectory} earnings · ${a.valuation?.assessment} valuation`;
      case 'news': return `sentiment ${a.sentiment > 0 ? '+' : ''}${Number(a.sentiment).toFixed(2)}${a.security_flag ? ' · flagged, ignored' : ''}`;
      case 'critic': return String(a.verdict ?? '').replaceAll('_', ' ');
      default: return '';
    }
  }

  constructor() {
    toObservable(this.id).pipe(switchMap((id) => this.api.recommendation(Number(id)))).subscribe({
      next: (r) => { this.d.set(r); this.error.set(''); },
      error: (e) => this.error.set(errorText(e)),
    });
  }

  addToSim(): void {
    const r = this.d();
    if (r) this.add.open(viewFromCard(r.card, r.id, r.status));
  }
}
