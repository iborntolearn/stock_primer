import { Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import { InrPipe } from '../core/format';
import { Card, Horizon, Verdict } from '../core/models';
import { ConfidenceMeter } from './confidence-meter';

export interface CardView {
  recoId: number | null; symbol: string; horizon: Horizon; verdict: Verdict; status: string;
  confidence: number | null; headline: string | null;
  levels: { entry_low: number | null; entry_high: number | null; stop: number | null; target: number | null };
}

export function viewFromCard(card: Card, recoId: number | null, status: string): CardView {
  return { recoId, symbol: card.symbol, horizon: card.horizon, verdict: card.verdict, status,
           confidence: card.confidence, headline: card.headline, levels: card.levels };
}

const STATUS_LABEL: Record<string, string> = {
  published: 'Published', active: 'Active', hitl_pending: 'Needs review', validated: 'Validated',
  rejected: 'Rejected', closed: 'Closed', expired: 'Expired', invalidated: 'Invalidated', draft: 'Draft',
};

/** Answer first: verdict, levels and confidence up top; details one click away (§9.1). */
@Component({
  selector: 'app-reco-card',
  imports: [RouterLink, InrPipe, ConfidenceMeter],
  template: `
    <article class="reco card">
      <header class="row spread">
        <div class="row">
          <strong class="sym">{{ v().symbol }}</strong>
          <span class="chip">{{ v().horizon }}</span>
          <span class="chip verdict" [class.good]="v().verdict === 'favourable'" [class.bad]="v().verdict === 'avoid'">
            {{ verdictIcon() }} {{ v().verdict }}
          </span>
          <span class="chip muted">{{ statusLabel() }}</span>
        </div>
        <app-confidence [value]="v().confidence" />
      </header>
      @if (v().headline) { <p class="headline">{{ v().headline }}</p> }
      <div class="levels num">
        <span><span class="muted">Entry</span> {{ v().levels.entry_low | inr }}–{{ v().levels.entry_high | inr }}</span>
        <span><span class="muted">Stop</span> {{ v().levels.stop | inr }}</span>
        <span><span class="muted">Target</span> {{ v().levels.target | inr }}</span>
      </div>
      <footer class="row">
        @if (v().recoId) { <a [routerLink]="['/recommendations', v().recoId]">Details</a> }
        @if (canAdd()) { <button type="button" (click)="addToSim.emit(v())">+ Sim</button> }
      </footer>
    </article>`,
  styles: `
    .reco { display: grid; gap: 8px; }
    .sym { font-size: 15px; }
    .verdict { text-transform: capitalize; }
    .headline { margin: 0; color: var(--text-secondary); }
    .levels { display: flex; gap: 4px 16px; flex-wrap: wrap; }
    .levels > span { white-space: nowrap; }
    footer { gap: 12px; }
  `,
})
export class RecoCard {
  v = input.required<CardView>();
  allowAdd = input(true);
  addToSim = output<CardView>();
  statusLabel = computed(() => STATUS_LABEL[this.v().status] ?? this.v().status);
  verdictIcon = computed(() => ({ favourable: '▲', neutral: '●', avoid: '▼' })[this.v().verdict]);
  canAdd = computed(() => this.allowAdd() && this.v().verdict === 'favourable' && this.v().levels.stop !== null);
}
