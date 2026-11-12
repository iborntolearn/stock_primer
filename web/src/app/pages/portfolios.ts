import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { InrPipe, PctPipe } from '../core/format';
import { PortfolioSummary } from '../core/models';

/** S6: simulated portfolios and the create form. */
@Component({
  selector: 'app-portfolios',
  imports: [FormsModule, RouterLink, InrPipe, PctPipe],
  template: `
    <h1>Simulated portfolios</h1>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    <div class="grid grid-2">
      @for (p of items(); track p.id) {
        <a class="card tile" [routerLink]="['/portfolios', p.id]">
          <div class="row spread"><strong>{{ p.name }}</strong><span class="chip">{{ p.type === 'auto_follow' ? 'Auto-follow' : 'Manual' }}</span></div>
          @if (p.metrics; as m) {
            <p class="nav num">₹{{ m.nav | inr: 0 }}</p>
            <p class="small secondary">{{ m.total_return_pct | pct }} since start
              @if (m.benchmark_return_pct !== null) { · {{ p.benchmark }} {{ m.benchmark_return_pct | pct }} }
              · max DD {{ m.max_drawdown_pct | pct: 1 : false }} · {{ p.open_positions }} open</p>
          } @else { <p class="muted small">₹{{ p.starting_cash | inr: 0 }} cash · no NAV yet (first snapshot after the next evaluation run)</p> }
        </a>
      } @empty { <p class="muted">{{ loading() ? 'Loading…' : 'No portfolios yet.' }}</p> }
    </div>
    <form class="card create stack" (ngSubmit)="create()">
      <h2>New portfolio</h2>
      <div class="row">
        <label>Name<input [(ngModel)]="name" name="name" required /></label>
        <label>Starting cash (₹)<input type="number" min="10000" step="10000" [(ngModel)]="cash" name="cash" required /></label>
        <label>Type<select [(ngModel)]="type" name="type">
          <option value="manual">Manual</option><option value="auto_follow">Auto-follow (takes every published pick)</option></select></label>
      </div>
      <div class="row"><button class="primary" type="submit" [disabled]="busy() || !name.trim()">Create</button></div>
    </form>`,
  styles: `
    .tile { color: inherit; display: block; }
    .tile:hover { text-decoration: none; border-color: var(--accent); }
    .nav { font-size: 22px; margin: 8px 0 0; }
    .create { margin-top: 16px; }
    .create .row label { flex: 1 1 180px; }
  `,
})
export class Portfolios {
  private api = inject(Api);
  items = signal<PortfolioSummary[]>([]);
  loading = signal(true);
  busy = signal(false);
  error = signal('');
  name = ''; cash = 1000000; type = 'manual';

  constructor() { this.load(); }

  load(): void {
    this.api.portfolios().subscribe({
      next: (r) => { this.items.set(r.items); this.loading.set(false); },
      error: (e) => { this.error.set(errorText(e)); this.loading.set(false); },
    });
  }

  create(): void {
    this.busy.set(true);
    this.api.createPortfolio({ name: this.name.trim(), starting_cash: this.cash, type: this.type }).subscribe({
      next: () => { this.busy.set(false); this.name = ''; this.load(); },
      error: (e) => { this.busy.set(false); this.error.set(errorText(e)); },
    });
  }
}
