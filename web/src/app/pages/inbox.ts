import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { InrPipe, IstPipe } from '../core/format';
import { HitlTask } from '../core/models';

const LABEL: Record<string, string> = {
  approve_reco: 'Approve pick', resolve_conflict: 'Resolve conflict', approve_map_change: 'Map change',
  data_anomaly: 'Data anomaly', review_thesis_break: 'Thesis break', approve_config: 'Config change',
  high_severity_event: 'High-severity event',
};

/** S9: review inbox, sorted by priority and due time. Default on expiry is always the safe option. */
@Component({
  selector: 'app-inbox',
  imports: [FormsModule, RouterLink, IstPipe, InrPipe],
  template: `
    <div class="row spread"><h1>Review inbox</h1>
      <label class="row status">Show <select [(ngModel)]="status" (change)="load()">
        <option value="pending">Pending</option><option value="all">All</option></select></label></div>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    <div class="layout">
      <ul class="list card" role="listbox" aria-label="Review tasks">
        @for (t of tasks(); track t.id) {
          <li role="option" [attr.aria-selected]="sel()?.id === t.id" [class.on]="sel()?.id === t.id" (click)="select(t)">
            <div class="row spread"><strong>{{ label(t.task_type) }}</strong><span class="chip small">P{{ t.priority }}</span></div>
            <div class="small secondary">{{ subject(t) }}</div>
            <div class="small muted">{{ t.status === 'pending' ? 'due ' + (t.due_at | ist) : t.status }}</div>
          </li>
        } @empty { <li class="muted empty">{{ loading() ? 'Loading…' : 'Nothing to review.' }}</li> }
      </ul>
      @if (sel(); as t) {
        <section class="card detail stack">
          <h2>{{ label(t.task_type) }} #{{ t.id }}</h2>
          <p><span class="muted">Why:</span> {{ t.reason }}</p>
          <p><span class="muted">AI suggests:</span> <strong>{{ t.ai_recommendation }}</strong>
            · <span class="muted">if undecided by {{ t.due_at | ist }}:</span> {{ t.default_on_expiry.replaceAll('_', ' ') }}</p>
          @if (card(); as c) {
            <div class="banner">
              <strong>{{ c.symbol }}</strong> {{ c.horizon }} · {{ c.verdict }} · conf {{ c.confidence | inr: 2 }}<br />
              {{ c.headline }}<br />
              <span class="num">Entry {{ c.levels.entry_low | inr }}–{{ c.levels.entry_high | inr }} · Stop {{ c.levels.stop | inr }} · Target {{ c.levels.target | inr }}</span>
              @if (t.subject_type === 'recommendation') { <br /><a [routerLink]="['/recommendations', t.subject_id]">Full card and evidence</a> }
            </div>
          } @else if (t.subject_type === 'recommendation') {
            <a [routerLink]="['/recommendations', t.subject_id]">Open recommendation #{{ t.subject_id }}</a>
          }
          @if (t.status === 'pending') {
            @if (t.allowed_actions.includes('edit') || t.allowed_actions.includes('adjust_stop')) {
              <div class="row">
                <label>New stop<input type="number" step="0.05" [(ngModel)]="editStop" /></label>
                @if (t.allowed_actions.includes('edit')) { <label>New target<input type="number" step="0.05" [(ngModel)]="editTarget" /></label> }
              </div>
            }
            <label>Note (optional)<textarea rows="2" [(ngModel)]="note"></textarea></label>
            <div class="row">
              @for (a of t.allowed_actions; track a) {
                <button [class.primary]="a === t.ai_recommendation" [class.danger]="a === 'reject' || a === 'close'"
                        [disabled]="busy()" (click)="decide(t, a)">{{ a.replace('_', ' ') }}</button>
              }
            </div>
            @if (decisionError()) { <p class="error">{{ decisionError() }}</p> }
          } @else {
            <p class="banner">Decided <strong>{{ t.decision?.action }}</strong> by {{ t.decided_by }} at {{ t.decided_at | ist }}.
              {{ (t.decision?.effects ?? []).join('; ') }}</p>
          }
        </section>
      } @else { <section class="card muted detail">Select a task.</section> }
    </div>`,
  styles: `
    .status { gap: 6px; font-size: 13px; color: var(--text-secondary); }
    .layout { display: grid; grid-template-columns: minmax(240px, 340px) 1fr; gap: 16px; align-items: start; }
    @media (max-width: 760px) { .layout { grid-template-columns: 1fr; } }
    .list { list-style: none; margin: 0; padding: 4px; }
    .list li { padding: 10px; border-radius: 8px; cursor: pointer; }
    .list li + li { border-top: 1px solid var(--grid); }
    .list li.on { background: var(--surface-2); outline: 1px solid var(--accent); }
    .list li.empty { cursor: default; }
    .detail p { margin: 0; }
    textarea { width: 100%; }
    button { text-transform: capitalize; }
  `,
})
export class Inbox {
  private api = inject(Api);
  status = 'pending';
  tasks = signal<HitlTask[]>([]);
  sel = signal<HitlTask | null>(null);
  loading = signal(true);
  busy = signal(false);
  error = signal('');
  decisionError = signal('');
  note = '';
  editStop: number | null = null;
  editTarget: number | null = null;
  card = computed(() => this.sel()?.payload?.card?.levels ? this.sel()!.payload.card : null);

  constructor() { this.load(); }

  label(t: string): string { return LABEL[t] ?? t; }
  subject(t: HitlTask): string {
    const c = t.payload?.card;
    return c?.symbol ? `${c.symbol} ${c.horizon} — ${c.headline ?? ''}` : t.payload?.symbol ?? t.reason ?? '';
  }

  load(): void {
    this.loading.set(true);
    this.api.tasks(this.status).subscribe({
      next: (r) => {
        this.tasks.set(r.items);
        this.loading.set(false);
        const keep = r.items.find((t) => t.id === this.sel()?.id);
        this.select(keep ?? r.items[0] ?? null);
      },
      error: (e) => { this.error.set(errorText(e)); this.loading.set(false); },
    });
  }

  select(t: HitlTask | null): void {
    this.sel.set(t);
    this.note = '';
    this.decisionError.set('');
    const lv = t?.payload?.card?.levels;
    this.editStop = lv?.stop ?? null;
    this.editTarget = lv?.target ?? null;
  }

  decide(t: HitlTask, action: string): void {
    const edits: Record<string, number> = {};
    const lv = t.payload?.card?.levels;
    if (action === 'edit') {
      if (this.editStop !== null && this.editStop !== lv?.stop) edits['stop'] = this.editStop;
      if (this.editTarget !== null && this.editTarget !== lv?.target) edits['target'] = this.editTarget;
    }
    if (action === 'adjust_stop' && this.editStop !== null) edits['stop'] = this.editStop;
    this.busy.set(true);
    this.api.decide(t.id, action, this.note || undefined, edits).subscribe({
      next: () => { this.busy.set(false); this.load(); },
      error: (e) => { this.busy.set(false); this.decisionError.set(errorText(e)); },
    });
  }
}
