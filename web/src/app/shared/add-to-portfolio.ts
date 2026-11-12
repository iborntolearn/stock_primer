import { Component, HostListener, Injectable, effect, inject, signal, untracked } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api, errorText } from '../core/api.service';
import { InrPipe, idempotencyKey } from '../core/format';
import { OrderResult, PortfolioSummary } from '../core/models';
import { CardView } from './reco-card';

/** S8: add a recommendation to a simulated portfolio. One dialog for the app, opened from any card. */
@Injectable({ providedIn: 'root' })
export class AddToPortfolioState {
  readonly card = signal<CardView | null>(null);
  open(c: CardView): void { this.card.set(c); }
  close(): void { this.card.set(null); }
}

@Component({
  selector: 'app-add-to-portfolio',
  imports: [FormsModule, InrPipe],
  template: `
    @if (s.card(); as c) {
      <div class="backdrop" (click)="close()"></div>
      <div class="modal card" role="dialog" aria-modal="true" [attr.aria-label]="'Add ' + c.symbol + ' to a portfolio'">
        <div class="row spread"><h2>Add {{ c.symbol }} ({{ c.horizon }}) to a simulated portfolio</h2>
          <button class="ghost" (click)="close()" aria-label="Close">✕</button></div>
        @if (result(); as r) {
          <p>Order #{{ r.order_id }} placed. Expected fill: <strong>{{ r.expected_fill }}</strong> at the open, plus slippage and costs.</p>
          <button class="primary" (click)="close()">Done</button>
        } @else {
          <form class="stack" (ngSubmit)="submit(c)">
            <label>Portfolio
              <select [(ngModel)]="portfolioId" name="pid" required>
                @for (p of portfolios(); track p.id) { <option [ngValue]="p.id">{{ p.name }} (cash ₹{{ p.cash | inr: 0 }})</option> }
              </select>
            </label>
            @if (loadingPortfolios()) { <p class="muted small">Loading portfolios…</p> }
            @else if (!portfolios().length) { <p class="muted small">No manual portfolio yet. Create one on the Portfolios page.</p> }
            <label>Amount (₹)<input type="number" min="1" step="1000" [(ngModel)]="amount" name="amount" required /></label>
            <div class="row">
              <label>Stop<input type="number" step="0.05" [(ngModel)]="stop" name="stop" /></label>
              <label>Target<input type="number" step="0.05" [(ngModel)]="target" name="target" /></label>
            </div>
            <p class="muted small">Fills at the next session's open, never today's close. Liquidity, position (10%) and sector (30%)
              caps apply; costs are added. Stop and target come from the card and can be changed here.</p>
            @if (error()) { <p class="error">{{ error() }}</p> }
            <div class="row"><button class="primary" type="submit" [disabled]="busy() || !portfolioId">Place paper order</button>
              <button type="button" (click)="close()">Cancel</button></div>
          </form>
        }
      </div>
    }`,
  styles: `
    .backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.3); z-index: 30; }
    .modal { position: fixed; z-index: 31; top: 10vh; left: 50%; transform: translateX(-50%); width: min(480px, calc(100vw - 32px)); }
    .row label { flex: 1; }
    select, input { width: 100%; }
  `,
})
export class AddToPortfolio {
  s = inject(AddToPortfolioState);
  private api = inject(Api);
  portfolios = signal<PortfolioSummary[]>([]);
  portfolioId: number | null = null;
  amount = 50000;
  stop: number | null = null;
  target: number | null = null;
  busy = signal(false);
  loadingPortfolios = signal(false);
  error = signal('');
  result = signal<OrderResult | null>(null);
  private key = idempotencyKey();

  constructor() {
    // Each time a card opens the dialog: reset the form and refresh the portfolio list.
    effect(() => {
      const c = this.s.card();
      if (!c) return;
      untracked(() => {
        this.stop = c.levels.stop;
        this.target = c.levels.target;
        this.result.set(null);
        this.error.set('');
        this.key = idempotencyKey();
        this.loadingPortfolios.set(true);
        this.api.portfolios().subscribe({
          next: (r) => {
            this.portfolios.set(r.items.filter((p) => p.type === 'manual'));
            this.portfolioId ??= this.portfolios()[0]?.id ?? null;
            this.loadingPortfolios.set(false);
          },
          error: (e) => { this.error.set(errorText(e)); this.loadingPortfolios.set(false); },
        });
      });
    });
  }

  submit(c: CardView): void {
    if (!this.portfolioId) return;
    this.busy.set(true);
    this.error.set('');
    const body = { reco_id: c.recoId, symbol: c.symbol, amount: this.amount, stop: this.stop, target: this.target, horizon: c.horizon };
    this.api.placeOrder(this.portfolioId, body, this.key).subscribe({
      next: (r) => { this.result.set(r); this.busy.set(false); },
      error: (e) => { this.error.set(errorText(e)); this.busy.set(false); },
    });
  }

  @HostListener('document:keydown.escape')
  close(): void {
    this.s.close();
  }
}
