import { Component, DestroyRef, ElementRef, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { Api, errorText } from '../core/api.service';
import { AnalysisResult, ChatJob } from '../core/models';
import { EvidenceChip } from '../shared/evidence';

interface Part { text?: string; ev?: string; }
interface Turn { role: 'you' | 'assistant'; text: string; parts: Part[]; intent?: string | null; results?: AnalysisResult[]; failed?: boolean; }

/** Split "... [E12] ... [E7, E9]" into text and evidence-chip parts. */
export function splitCitations(text: string): Part[] {
  const parts: Part[] = [];
  let last = 0;
  for (const m of text.matchAll(/\[(E\d+(?:\s*,\s*E\d+)*)\]/g)) {
    if (m.index! > last) parts.push({ text: text.slice(last, m.index) });
    for (const id of m[1].split(',')) parts.push({ ev: id.trim() });
    last = m.index! + m[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last) });
  return parts;
}

/** S4 (chat part): grounded Q&A. Answers come from tools and stored evidence and cite it; nothing here is published. */
@Component({
  selector: 'app-ask',
  imports: [FormsModule, RouterLink, EvidenceChip],
  template: `
    <h1>Ask</h1>
    <p class="secondary small">Questions are answered only from stored data and live tools, with evidence cited.
      Asking to analyze a stock or find ideas runs the same research as the Analyze page.</p>
    <section class="card log" aria-live="polite">
      @for (t of turns(); track $index) {
        <div class="turn" [class.you]="t.role === 'you'">
          <div class="who small muted">{{ t.role === 'you' ? 'You' : 'stock_primer' }}@if (t.intent) { · {{ t.intent }} }</div>
          <div class="bubble" [class.bad]="t.failed">
            @for (p of t.parts; track $index) {
              @if (p.ev) { <app-evidence-chip [id]="p.ev" /> } @else { <span class="txt">{{ p.text }}</span> }
            }
          </div>
          @for (r of t.results ?? []; track r.symbol + r.horizon) {
            <p class="small">{{ r.symbol }} {{ r.horizon }}: <strong>{{ r.verdict }}</strong> ({{ r.status }})
              @if (r.reco_id) { · <a [routerLink]="['/recommendations', r.reco_id]">details</a> }</p>
          }
        </div>
      } @empty {
        <p class="muted">Try “Why did metals fall this week?”, “What's the latest on HDFCBANK results?” or “Analyze INFY short term”.</p>
      }
      @if (status()) { <p class="muted small status">{{ status() }}</p> }
      <div #end></div>
    </section>
    <form class="row ask" (ngSubmit)="send()">
      <label class="grow"><span class="sr-only">Question</span>
        <input [(ngModel)]="message" name="m" maxlength="2000" placeholder="Ask about a stock, sector or event" [disabled]="busy()" /></label>
      <button class="primary" type="submit" [disabled]="busy() || !message.trim()">{{ busy() ? 'Thinking…' : 'Ask' }}</button>
    </form>
    @if (error()) { <p class="banner bad">{{ error() }}</p> }
    <p class="muted small">Decision-support only. Not investment advice.</p>`,
  styles: `
    .log { min-height: 240px; max-height: 60vh; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }
    .turn { max-width: 80ch; }
    .turn.you { align-self: flex-end; text-align: right; }
    .bubble { white-space: pre-wrap; line-height: 1.5; }
    .turn.you .bubble { background: var(--div-mid); border-radius: var(--radius); padding: 8px 12px; display: inline-block; text-align: left; }
    .bubble.bad { color: var(--critical-text); }
    .bubble app-evidence-chip { margin: 0 2px; }
    .ask { margin-top: 12px; }
    .grow { flex: 1 1 260px; }
    .grow input { width: 100%; }
    .status { margin: 0; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    .banner { margin-top: 12px; }
  `,
})
export class Ask {
  private api = inject(Api);
  private end = viewChild<ElementRef<HTMLElement>>('end');
  message = '';
  turns = signal<Turn[]>([]);
  busy = signal(false);
  status = signal('');
  error = signal('');
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor() { inject(DestroyRef).onDestroy(() => this.clear()); }

  send(): void {
    const text = this.message.trim();
    if (!text || this.busy()) return;
    const history = this.turns().slice(-6).map((t) => `${t.role === 'you' ? 'User' : 'Assistant'}: ${t.text}`);
    this.push({ role: 'you', text, parts: [{ text }] });
    this.message = '';
    this.busy.set(true); this.error.set(''); this.status.set('Thinking…');
    this.api.chat(text, history).subscribe({
      next: ({ job_id }) => this.poll(job_id),
      error: (e) => this.fail(errorText(e)),
    });
  }

  private poll(id: number): void {
    this.clear();
    this.timer = setInterval(() => this.api.chatJob(id).subscribe({
      next: (j) => this.update(j),
      error: (e) => this.fail(errorText(e)),
    }), 800);
  }

  private update(j: ChatJob): void {
    const last = j.events.at(-1);
    if (last) this.status.set(last.message);
    if (j.status === 'running') return;
    this.clear();
    if (j.status === 'failed' || !j.result) { this.fail(j.error ?? 'No answer'); return; }
    const reply = j.result.reply || 'No answer.';
    this.push({ role: 'assistant', text: reply, parts: splitCitations(reply), intent: j.result.intent, results: j.result.results });
    this.busy.set(false); this.status.set('');
  }

  private fail(msg: string): void {
    this.clear();
    this.push({ role: 'assistant', text: msg, parts: [{ text: msg }], failed: true });
    this.busy.set(false); this.status.set('');
  }

  private push(t: Turn): void {
    this.turns.update((x) => [...x, t]);
    queueMicrotask(() => this.end()?.nativeElement.scrollIntoView?.({ block: 'end' }));
  }

  private clear(): void { if (this.timer) { clearInterval(this.timer); this.timer = null; } }
}
