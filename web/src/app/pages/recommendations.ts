import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { InrPipe, IstPipe, PctPipe } from '../core/format';
import { RecoRow } from '../core/models';

/** S2: all recommendations with filters; outcome once scored. */
@Component({
  selector: 'app-recommendations',
  imports: [FormsModule, RouterLink, InrPipe, PctPipe, IstPipe],
  template: `
    <div class="row spread"><h1>Recommendations</h1>
      <button class="ghost" (click)="exportCsv()">Export CSV</button></div>
    <form class="row filters" (ngSubmit)="load()">
      <label>Horizon<select [(ngModel)]="horizon" name="h" (change)="load()">
        <option value="">All</option><option>short</option><option>medium</option><option>long</option></select></label>
      <label>Status<select [(ngModel)]="status" name="s" (change)="load()">
        <option value="">All</option><option value="published,active">Open</option><option value="hitl_pending">Needs review</option>
        <option value="closed,expired,invalidated">Closed</option><option value="rejected">Rejected</option></select></label>
      <label>Symbol<input [(ngModel)]="symbol" name="sym" placeholder="e.g. HDFCBANK" /></label>
      <button type="submit">Apply</button>
    </form>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    <div class="card table-wrap">
      <table>
        <thead><tr><th>Symbol</th><th>Horizon</th><th>Verdict</th><th class="num">Score</th><th class="num">Conf.</th>
          <th class="num">Entry</th><th class="num">Stop</th><th class="num">Target</th><th>Status</th><th>Published</th><th class="num">Result</th></tr></thead>
        <tbody>
          @for (r of rows(); track r.id) {
            <tr>
              <td><a [routerLink]="['/recommendations', r.id]"><strong>{{ r.symbol }}</strong></a></td>
              <td>{{ r.horizon }}</td><td>{{ r.verdict }}</td>
              <td class="num">{{ r.final_score | inr: 1 }}</td><td class="num">{{ r.confidence | inr: 2 }}</td>
              <td class="num">{{ r.entry_low | inr }}–{{ r.entry_high | inr }}</td>
              <td class="num">{{ r.stop | inr }}</td><td class="num">{{ r.target | inr }}</td>
              <td><span class="chip">{{ r.status }}</span></td>
              <td>{{ r.published_at | ist: true }}</td>
              <td class="num">@if (r.outcome; as o) { {{ o.ret_pct | pct }} <span class="muted small">α {{ o.alpha_pct | pct }}</span> }</td>
            </tr>
          } @empty { <tr><td colspan="11" class="muted">{{ loading() ? 'Loading…' : 'No recommendations match.' }}</td></tr> }
        </tbody>
      </table>
    </div>`,
  styles: `.filters { margin-bottom: 12px; align-items: end; }`,
})
export class Recommendations {
  private api = inject(Api);
  horizon = ''; status = ''; symbol = '';
  rows = signal<RecoRow[]>([]);
  loading = signal(true);
  error = signal('');

  constructor() { this.load(); }

  exportCsv(): void {
    const q = new URLSearchParams({ ...(this.horizon ? { horizon: this.horizon } : {}), ...(this.status ? { status: this.status } : {}) });
    this.api.download(`/export/recommendations.csv${q.size ? '?' + q : ''}`, 'recommendations.csv').subscribe({ error: (e) => this.error.set(errorText(e)) });
  }

  load(): void {
    this.loading.set(true);
    this.api.recommendations({ horizon: this.horizon, status: this.status, symbol: this.symbol.trim().toUpperCase(), limit: 200 })
      .subscribe({ next: (r) => { this.rows.set(r.items); this.loading.set(false); this.error.set(''); },
                   error: (e) => { this.error.set(errorText(e)); this.loading.set(false); } });
  }
}
