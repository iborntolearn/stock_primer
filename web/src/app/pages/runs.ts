import { Component, computed, inject, input, signal } from '@angular/core';
import { toObservable } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { switchMap } from 'rxjs';

import { Api, errorText } from '../core/api.service';
import { InrPipe, IstPipe } from '../core/format';
import { RunDetail as Detail, RunRow } from '../core/models';

/** S11: runs and audit. */
@Component({
  selector: 'app-runs',
  imports: [RouterLink, IstPipe, InrPipe],
  template: `
    <h1>Runs &amp; audit</h1>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    <div class="card table-wrap">
      <table>
        <thead><tr><th>#</th><th>Type</th><th>Trigger</th><th>Status</th><th>Decision time</th><th class="num">Duration</th><th class="num">LLM $</th></tr></thead>
        <tbody>@for (r of rows(); track r.id) {
          <tr><td><a [routerLink]="['/runs', r.id]">{{ r.id }}</a></td><td>{{ r.run_type }}</td><td class="small">{{ r.trigger }}</td>
            <td><span class="chip" [class.good]="r.status === 'succeeded'" [class.bad]="r.status === 'failed'" [class.warn]="r.status === 'degraded'">{{ r.status }}</span></td>
            <td>{{ r.decision_time | ist }}</td><td class="num">{{ duration(r) }}</td><td class="num">{{ r.spent_usd | inr: 3 }}</td></tr>
        } @empty { <tr><td colspan="7" class="muted">No runs yet.</td></tr> }</tbody>
      </table>
    </div>`,
})
export class Runs {
  private api = inject(Api);
  rows = signal<RunRow[]>([]);
  error = signal('');
  constructor() {
    this.api.runs(100).subscribe({ next: (r) => this.rows.set(r.items), error: (e) => this.error.set(errorText(e)) });
  }
  duration(r: RunRow): string {
    if (!r.finished_at) return r.status === 'running' ? 'running' : '–';
    const s = (new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000;
    return s < 120 ? `${s.toFixed(0)}s` : `${(s / 60).toFixed(1)}m`;
  }
}

@Component({
  selector: 'app-run-detail',
  imports: [RouterLink, IstPipe, InrPipe],
  template: `
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    @if (d(); as r) {
      <h1>Run #{{ r.id }} · {{ r.run_type }}</h1>
      <p class="secondary">{{ r.status }} · decided {{ r.decision_time | ist }} · config v{{ r.config_version }} · prompts {{ r.prompt_bundle_version }}
        · LLM spend \${{ r.spent_usd | inr: 3 }} · {{ r.trigger }}</p>
      @if (r.error) { <pre class="banner warn small">{{ errorJson() }}</pre> }
      @if (r.recommendations.length) {
        <p>Recommendations: @for (id of r.recommendations; track id) { <a [routerLink]="['/recommendations', id]">#{{ id }}</a>&nbsp; }</p>
      }
      <div class="grid grid-2">
        <section class="card table-wrap">
          <h3>LLM calls ({{ r.llm_calls.length }}) · {{ tokens() }}</h3>
          <table><thead><tr><th>Call</th><th>Agent</th><th>Model</th><th class="num">In/Out</th><th class="num">$</th><th class="num">ms</th><th>Valid.</th></tr></thead>
            <tbody>@for (c of r.llm_calls; track $index) {
              <tr><td>{{ c.call_id }}</td><td>{{ c.agent }}</td><td class="small">{{ c.model }}</td><td class="num">{{ c.tokens_in }}/{{ c.tokens_out }}</td>
                <td class="num">{{ c.cost_usd | inr: 4 }}</td><td class="num">{{ c.latency_ms }}</td>
                <td><span class="chip" [class.bad]="c.validation === 'failed'">{{ c.validation }}{{ c.retries ? ' ·' + c.retries : '' }}</span></td></tr>
            } @empty { <tr><td colspan="7" class="muted">None recorded.</td></tr> }</tbody></table>
        </section>
        <section class="card table-wrap">
          <h3>Tool calls ({{ r.tool_calls.length }}, {{ failed() }} failed)</h3>
          <table><thead><tr><th>Tool</th><th>Agent</th><th>Result</th><th class="num">ms</th></tr></thead>
            <tbody>@for (t of r.tool_calls; track $index) {
              <tr><td>{{ t.tool }}</td><td>{{ t.agent }}</td>
                <td>@if (t.ok) { <span class="small">{{ t.evidence }}</span> } @else { <span class="chip bad">{{ t.error_code }}</span> }</td>
                <td class="num">{{ t.latency_ms }}</td></tr>
            }</tbody></table>
        </section>
      </div>
    } @else if (!error()) { <p class="skeleton">Loading…</p> }`,
  styles: `pre { white-space: pre-wrap; margin: 0 0 12px; } .grid { margin-top: 12px; }`,
})
export class RunDetail {
  id = input.required<string>();
  private api = inject(Api);
  d = signal<Detail | null>(null);
  error = signal('');
  failed = computed(() => this.d()?.tool_calls.filter((t) => !t.ok).length ?? 0);
  tokens = computed(() => {
    const c = this.d()?.llm_calls ?? [];
    return `${c.reduce((a, x) => a + (x.tokens_in ?? 0), 0).toLocaleString('en-IN')} in / ${c.reduce((a, x) => a + (x.tokens_out ?? 0), 0).toLocaleString('en-IN')} out`;
  });
  errorJson = computed(() => JSON.stringify(this.d()?.error, null, 2));
  constructor() {
    toObservable(this.id).pipe(switchMap((id) => this.api.run(Number(id)))).subscribe({
      next: (r) => this.d.set(r), error: (e) => this.error.set(errorText(e)),
    });
  }
}
