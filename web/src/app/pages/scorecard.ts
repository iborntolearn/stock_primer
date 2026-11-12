import { Component, computed, inject, input, signal } from '@angular/core';
import { KeyValuePipe } from '@angular/common';

import { Api, errorText } from '../core/api.service';
import { InrPipe, PctPipe } from '../core/format';
import { Scorecard as SC, ScoreSummary } from '../core/models';

/** A compact outcome table: one row per group, sample size first, thin samples greyed. */
@Component({
  selector: 'app-summary-table',
  imports: [PctPipe],
  template: `
    <table>
      <thead><tr><th>{{ label() }}</th><th class="num">n</th><th class="num">Positive alpha</th><th class="num">Avg alpha</th><th class="num">Avg return</th></tr></thead>
      <tbody>@for (r of rows(); track r.key) {
        <tr [class.thin]="!r.s.sufficient_sample"><td>{{ r.key }}</td><td class="num">{{ r.s.n }}</td>
          <td class="num">{{ rate(r.s.positive_alpha_rate) }}</td><td class="num">{{ r.s.avg_alpha_pct | pct }}</td>
          <td class="num">{{ r.s.avg_return_pct | pct }}</td></tr>
      } @empty { <tr><td colspan="5" class="muted">Nothing scored yet.</td></tr> }</tbody>
    </table>`,
  styles: `tr.thin td { color: var(--text-muted); }`,
})
export class SummaryTable {
  label = input.required<string>();
  data = input.required<Record<string, ScoreSummary> | { key: string; s: ScoreSummary }[]>();
  rows = computed(() => {
    const d = this.data();
    return Array.isArray(d) ? d : Object.entries(d).map(([key, s]) => ({ key, s }));
  });
  rate(x: number | null): string { return x === null ? '–' : `${(x * 100).toFixed(0)}%`; }
}

const COHORT_LABEL: Record<string, string> = {
  published: 'Published (approved or auto)', human_rejected: 'Rejected in review', expired: 'Expired undecided',
};

/** S10: recommender performance. Sample sizes beside every number; nothing is judged below the minimum sample. */
@Component({
  selector: 'app-scorecard',
  imports: [InrPipe, PctPipe, KeyValuePipe, SummaryTable],
  template: `
    <h1>Scorecard</h1>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    @if (sc(); as s) {
      @if (!s.overall.sufficient_sample) {
        <p class="banner warn">Only {{ s.overall.n }} scored recommendation{{ s.overall.n === 1 ? '' : 's' }} so far (minimum {{ s.min_sample }}).
          Treat every number here as noise until the sample grows.</p>
      }
      <section class="card value-add">
        <h3>Do the agents add value over the quant engine?</h3>
        @if (s.value_add; as v) {
          <div class="row tiles">
            <div class="tile"><span class="muted small">Auto-follow (agents + review)</span>
              <span class="big num">{{ v.auto_follow_return_pct | pct }}</span><span class="small muted">{{ v.auto_follow_trades }} closed trades</span></div>
            <div class="tile"><span class="muted small">Quant-only shadow</span>
              <span class="big num">{{ v.quant_shadow_return_pct | pct }}</span><span class="small muted">{{ v.quant_shadow_trades }} closed trades</span></div>
            <div class="tile"><span class="muted small">Difference</span>
              <span class="big num" [class.pos]="v.agents_minus_quant_pp > 0" [class.neg]="v.agents_minus_quant_pp < 0">
                {{ v.agents_minus_quant_pp > 0 ? '+' : '' }}{{ v.agents_minus_quant_pp | inr: 2 }} pp</span>
              <span class="small muted">{{ v.sufficient_sample ? 'enough trades to judge' : 'too few trades to judge (min ' + s.min_sample + ')' }}</span></div>
          </div>
        } @else {
          <p class="muted">Needs both the auto-follow and the quant-only shadow portfolio to have NAV history.</p>
        }
        <p class="small muted">The shadow portfolio buys the quant screen's top picks with the same sizing and limits, with no agents, critic or review.</p>
      </section>

      <section class="card table-wrap">
        <h3>By horizon</h3>
        <table>
          <thead><tr><th>Horizon</th><th class="num">n</th><th class="num">Positive alpha</th><th class="num">Target hit</th>
            <th class="num">Stopped</th><th class="num">Avg return</th><th class="num">Avg alpha</th><th class="num">Avg days</th></tr></thead>
          <tbody>
            <tr class="overall"><td>All</td>
              <td class="num">{{ s.overall.n }}</td><td class="num">{{ rate(s.overall.positive_alpha_rate) }}</td><td class="num">{{ rate(s.overall.target_hit_rate) }}</td>
              <td class="num">{{ rate(s.overall.stop_hit_rate) }}</td><td class="num">{{ s.overall.avg_return_pct | pct }}</td>
              <td class="num">{{ s.overall.avg_alpha_pct | pct }}</td><td class="num">{{ s.overall.avg_days_held ?? '–' }}</td></tr>
            @for (h of s.by_horizon | keyvalue; track h.key) {
              <tr [class.thin]="!h.value.sufficient_sample"><td>{{ h.key }}</td>
                <td class="num">{{ h.value.n }}</td><td class="num">{{ rate(h.value.positive_alpha_rate) }}</td><td class="num">{{ rate(h.value.target_hit_rate) }}</td>
                <td class="num">{{ rate(h.value.stop_hit_rate) }}</td><td class="num">{{ h.value.avg_return_pct | pct }}</td>
                <td class="num">{{ h.value.avg_alpha_pct | pct }}</td><td class="num">{{ h.value.avg_days_held ?? '–' }}</td></tr>
            }
          </tbody>
        </table>
      </section>
      <div class="grid grid-2">
        <section class="card table-wrap">
          <h3>Calibration</h3>
          <p class="small muted">Higher-confidence buckets should show better outcomes.</p>
          <table><thead><tr><th>Confidence</th><th class="num">n</th><th class="num">Mean conf.</th><th class="num">Positive alpha</th></tr></thead>
            <tbody>@for (b of s.calibration; track b.bucket) {
              <tr><td>{{ b.bucket }}</td><td class="num">{{ b.n }}</td><td class="num">{{ b.mean_confidence | inr: 2 }}</td>
                <td class="num">{{ rate(b.positive_alpha_rate) }}</td></tr>
            } @empty { <tr><td colspan="4" class="muted">No scored recommendations yet.</td></tr> }</tbody></table>
        </section>
        <section class="card">
          <h3>Honesty and cost</h3>
          <p>Runs with no picks: {{ s.abstention.runs_without_picks }} of {{ s.abstention.runs }}
            ({{ rate(s.abstention.rate) }}). A recommender that always finds picks is suspect.</p>
          <p>LLM spend \${{ s.cost.llm_spend_usd | inr }} · {{ s.cost.published }} published ·
            \${{ s.cost.usd_per_published === null ? '–' : (s.cost.usd_per_published | inr: 3) }} per published pick</p>
          <h3>Portfolios</h3>
          @for (p of s.portfolios; track p.portfolio_id) {
            <p class="row spread"><span>{{ p.name }} <span class="chip">{{ p.type }}</span></span>
              @if (p.metrics; as m) { <span class="num">{{ m.total_return_pct | pct }} · alpha {{ m.alpha_pct | pct }}</span> }
              @else { <span class="muted">no NAV yet</span> }</p>
          }
        </section>
      </div>
      <div class="grid grid-2">
        <section class="card table-wrap">
          <h3>Does review add value?</h3>
          <p class="small muted">Rejected and expired picks are followed on paper as if taken. If rejected picks do as well as published ones, review is not helping.</p>
          <app-summary-table label="Cohort" [data]="cohorts()" />
        </section>
        <section class="card table-wrap">
          <h3>Signal attribution</h3>
          <p class="small muted">Outcomes when each signal raised vs lowered the score.</p>
          <table><thead><tr><th>Signal</th><th class="num">Raised: n</th><th class="num">avg alpha</th><th class="num">Lowered: n</th><th class="num">avg alpha</th></tr></thead>
            <tbody>@for (a of s.attribution.by_agent; track a.source) {
              <tr><td>{{ a.source }}</td><td class="num">{{ a.raised.n }}</td><td class="num">{{ a.raised.avg_alpha_pct | pct }}</td>
                <td class="num">{{ a.lowered.n }}</td><td class="num">{{ a.lowered.avg_alpha_pct | pct }}</td></tr>
            } @empty { <tr><td colspan="5" class="muted">Nothing scored yet.</td></tr> }</tbody></table>
        </section>
        <section class="card table-wrap">
          <h3>By market regime at issue</h3>
          <app-summary-table label="Regime" [data]="s.attribution.by_regime" />
        </section>
        <section class="card table-wrap">
          <h3>By sector</h3>
          <app-summary-table label="Sector" [data]="s.attribution.by_sector" />
        </section>
      </div>
    } @else if (!error()) { <p class="skeleton">Loading…</p> }`,
  styles: `
    section + .grid, .banner + section { margin-top: 16px; }
    .grid { margin-top: 16px; }
    tr.overall td { font-weight: 600; }
    tr.thin td { color: var(--text-muted); }
    .value-add { margin-bottom: 16px; }
    .tiles { gap: 16px; flex-wrap: wrap; }
    .tile { display: flex; flex-direction: column; gap: 2px; min-width: 180px; flex: 1 1 180px; }
    .big { font-size: 24px; font-weight: 600; }
    .big.pos { color: var(--good-text); } .big.neg { color: var(--critical-text); }
  `,
})
export class Scorecard {
  private api = inject(Api);
  sc = signal<SC | null>(null);
  error = signal('');
  cohorts = computed(() => Object.entries(this.sc()?.human_review ?? {})
    .map(([k, s]) => ({ key: COHORT_LABEL[k] ?? k, s })));
  constructor() { this.api.scorecard().subscribe({ next: (s) => this.sc.set(s), error: (e) => this.error.set(errorText(e)) }); }
  rate(x: number | null): string { return x === null ? '–' : `${(x * 100).toFixed(0)}%`; }
}
export type { ScoreSummary };
