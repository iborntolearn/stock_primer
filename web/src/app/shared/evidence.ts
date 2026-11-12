import { JsonPipe } from '@angular/common';
import { Component, HostListener, Injectable, inject, input, signal } from '@angular/core';

import { Api, errorText } from '../core/api.service';
import { IstPipe } from '../core/format';
import { Evidence } from '../core/models';

/** One drawer for the whole app; any EvidenceChip opens it. */
@Injectable({ providedIn: 'root' })
export class EvidenceDrawerState {
  private api = inject(Api);
  readonly open = signal(false);
  readonly item = signal<Evidence | null>(null);
  readonly error = signal('');

  show(id: string): void {
    this.open.set(true);
    this.item.set(null);
    this.error.set('');
    this.api.evidence(id).subscribe({ next: (e) => this.item.set(e), error: (e) => this.error.set(errorText(e)) });
  }
}

@Component({
  selector: 'app-evidence-chip',
  template: `<button type="button" class="chip ev" (click)="drawer.show(id())" [title]="'Open evidence ' + id()">{{ id() }}</button>`,
  styles: `.ev { cursor: pointer; font-family: ui-monospace, monospace; padding: 0 6px; }`,
})
export class EvidenceChip {
  id = input.required<string>();
  drawer = inject(EvidenceDrawerState);
}

@Component({
  selector: 'app-evidence-drawer',
  imports: [JsonPipe, IstPipe],
  template: `
    @if (s.open()) {
      <div class="backdrop" (click)="s.open.set(false)"></div>
      <aside class="drawer card" role="dialog" aria-label="Evidence">
        <div class="row spread"><h2>Evidence {{ s.item()?.id }}</h2><button class="ghost" (click)="s.open.set(false)" aria-label="Close">✕</button></div>
        @if (s.error()) { <p class="error">{{ s.error() }}</p> }
        @if (s.item(); as e) {
          <dl>
            <dt>Kind</dt><dd>{{ e.kind }}</dd>
            <dt>Source</dt><dd>{{ e.source }} <span class="muted">{{ e.source_ref }}</span></dd>
            <dt>As of</dt><dd>{{ e.as_of | ist }}</dd>
          </dl>
          @if (e.untrusted) { <p class="banner warn small">External text. Shown as data; the agents treat it as untrusted.</p> }
          <pre>{{ e.payload | json }}</pre>
        } @else if (!s.error()) { <p class="skeleton">Loading…</p> }
      </aside>
    }`,
  styles: `
    .backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.25); z-index: 20; }
    .drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(560px, 100vw); z-index: 21; overflow: auto; border-radius: 0; }
    dl { display: grid; grid-template-columns: 80px 1fr; gap: 4px 12px; margin: 0 0 12px; }
    dt { color: var(--text-muted); } dd { margin: 0; }
    pre { background: var(--surface-2); border-radius: 8px; padding: 12px; font-size: 12px; overflow: auto; white-space: pre-wrap; word-break: break-word; }
  `,
})
export class EvidenceDrawer {
  s = inject(EvidenceDrawerState);
  @HostListener('document:keydown.escape') close(): void { this.s.open.set(false); }
}
