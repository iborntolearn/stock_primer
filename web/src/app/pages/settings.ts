import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Api, errorText } from '../core/api.service';
import { AlertRow } from '../core/models';
import { Theme, ThemeService } from '../core/theme';

/** S12 (v1 slice): API token, theme, kill switch, and the active config (read-only). */
@Component({
  selector: 'app-settings',
  imports: [FormsModule],
  template: `
    <h1>Settings</h1>
    <div class="grid grid-2">
      <section class="card stack">
        <h2>Kill switch</h2>
        <p class="secondary">When on, nothing is auto-published: every pick goes to the review inbox.</p>
        @if (killSwitch() !== null) {
          <div class="row">
            <span class="chip" [class.bad]="killSwitch()">{{ killSwitch() ? 'Publishing paused' : 'Publishing allowed' }}</span>
            <button [class.danger]="!killSwitch()" (click)="toggle()" [disabled]="busy()">{{ killSwitch() ? 'Resume publishing' : 'Pause publishing' }}</button>
          </div>
        }
        @if (error()) { <p class="error">{{ error() }}</p> }
      </section>
      <section class="card stack">
        <h2>Connection</h2>
        <label>API token (if the server sets STOCK_PRIMER_API_TOKEN)
          <input type="password" [(ngModel)]="token" autocomplete="off" /></label>
        <div class="row"><button class="primary" (click)="saveToken()">Save</button>
          @if (saved()) { <span class="muted small">Saved in this browser.</span> }</div>
        <form ngNoForm method="post" action="/auth/logout" class="row"><button type="submit" class="ghost">Sign out</button>
          <span class="muted small">Only when signed in through Caddy.</span></form>
        <label>Theme
          <select [ngModel]="theme.theme()" (ngModelChange)="theme.set($event)">
            <option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option>
          </select></label>
      </section>
      <section class="card stack wide">
        <h2>Autonomy</h2>
        <p class="secondary small">L0 manual · L1 every pick is reviewed · L2 picks above the confidence bar publish automatically, the rest are reviewed ·
          L3 everything that passes validation publishes. Raise a horizon to L2 only after the scorecard shows the agents add value
          with enough closed trades. A change takes effect after you approve it in the review inbox.</p>
        <table>
          <thead><tr><th>Horizon</th><th>Current</th><th>Request</th></tr></thead>
          <tbody>@for (h of horizons; track h) {
            <tr><td>{{ h }}</td><td class="num">L{{ level(h) }}</td>
              <td><div class="row">
                <select [(ngModel)]="wanted[h]" [attr.aria-label]="'New level for ' + h">
                  @for (l of [0, 1, 2, 3]; track l) { <option [ngValue]="l">L{{ l }}</option> }</select>
                <button (click)="requestAutonomy(h)" [disabled]="busy() || wanted[h] === level(h)">Request change</button>
              </div></td></tr>
          }</tbody>
        </table>
        <label>Note for the approval (optional)<input [(ngModel)]="note" maxlength="300" /></label>
        @if (autonomyMsg()) { <p class="small" role="status">{{ autonomyMsg() }}</p> }
      </section>
      <section class="card stack wide">
        <h2>Intraday stop alerts <span class="muted small">optional</span></h2>
        <p class="secondary small">Every 15 minutes in market hours, checks delayed quotes for open simulated positions and live picks,
          and sends a Telegram alert when a price comes within the set distance of its stop, touches the stop, or reaches its target.
          Each alert fires once a day. Alerts only: simulated portfolios still act at the end-of-day close.</p>
        <div class="row">
          <label class="check"><input type="checkbox" [(ngModel)]="alertsOn" name="ao" /> Alerts on</label>
          <label>Warn within (% above stop)<input type="number" min="0.1" max="10" step="0.1" [(ngModel)]="proximity" name="px" class="num narrow" /></label>
          <button (click)="saveAlerts()" [disabled]="busy()">Save</button>
          @if (alertsMsg()) { <span class="small" role="status">{{ alertsMsg() }}</span> }
        </div>
        @if (recentAlerts().length) {
          <table class="small"><thead><tr><th>Date</th><th>Alert</th><th>Symbol</th><th class="num">Price</th><th class="num">Level</th><th>For</th></tr></thead>
            <tbody>@for (a of recentAlerts(); track a.id) {
              <tr><td>{{ a.date }}</td><td>{{ alertLabel(a.kind) }}</td><td>{{ a.symbol }}</td><td class="num">{{ a.price }}</td><td class="num">{{ a.level }}</td><td>{{ a.label }}</td></tr>
            }</tbody></table>
        } @else { <p class="small muted">No alerts in the last 7 days.</p> }
      </section>
      <section class="card stack wide">
        <h2>Active configuration <span class="muted small">v{{ config()?.['version'] }}</span></h2>
        <p class="small muted">Changes to weights, gates, budgets and autonomy are versioned and approval-gated.</p>
        <dl>
          @for (k of keys; track k) { <dt>{{ k }}</dt><dd class="num">{{ show(config()?.[k]) }}</dd> }
        </dl>
      </section>
    </div>`,
  styles: `
    .wide { grid-column: 1 / -1; }
    .narrow { width: 80px; }
    .check { display: flex; flex-direction: row; align-items: center; gap: 4px; color: var(--text-primary); }
    dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 0; }
    dt { color: var(--text-muted); } dd { margin: 0; word-break: break-word; }
  `,
})
export class Settings {
  private api = inject(Api);
  theme = inject(ThemeService);
  token = this.api.token();
  saved = signal(false);
  config = signal<Record<string, any> | null>(null);
  killSwitch = signal<boolean | null>(null);
  busy = signal(false);
  error = signal('');
  keys = ['autonomy', 'publish_confidence_min', 'max_published_per_horizon', 'top_k_per_horizon', 'budget_usd_per_run',
          'budget_usd_per_day', 'budget_usd_per_month', 'adjustment_cap', 'rr_floor', 'tiers', 'universe'];

  horizons = ['short', 'medium', 'long'];
  wanted: Record<string, number> = {};
  note = '';
  autonomyMsg = signal('');
  alertsOn = false;
  proximity = 1.5;
  alertsMsg = signal('');
  recentAlerts = signal<AlertRow[]>([]);

  alertLabel(kind: string): string {
    return ({ near_stop: '⚠ near stop', stop_breach: '⛔ stop touched', target_hit: '🎯 target' } as Record<string, string>)[kind] ?? kind;
  }

  saveAlerts(): void {
    this.busy.set(true); this.alertsMsg.set('');
    this.api.setAlerts(this.alertsOn, Number(this.proximity)).subscribe({
      next: (r) => { this.busy.set(false); this.alertsMsg.set(r.intraday_alerts ? `On, warning within ${r.stop_proximity_pct}% of a stop.` : 'Off.'); this.load(); },
      error: (e) => { this.busy.set(false); this.alertsMsg.set(errorText(e)); },
    });
  }

  constructor() { this.load(); }

  level(h: string): number { return Number(this.config()?.['autonomy']?.[`publish_${h}`] ?? 0); }

  requestAutonomy(h: string): void {
    this.busy.set(true); this.autonomyMsg.set('');
    this.api.requestAutonomy(`publish_${h}`, this.wanted[h], this.note.trim()).subscribe({
      next: (r) => { this.busy.set(false); this.autonomyMsg.set(`Requested: ${h} L${this.level(h)} → L${this.wanted[h]}. Approve task #${r.task_id} in the review inbox to apply it.`); },
      error: (e) => { this.busy.set(false); this.autonomyMsg.set(errorText(e)); },
    });
  }

  load(): void {
    this.api.alerts().subscribe({ next: (r) => this.recentAlerts.set(r.items), error: () => {} });
    this.api.config().subscribe({
      next: (c) => {
        this.config.set(c); this.killSwitch.set(!!c['kill_switch']);
        for (const h of this.horizons) this.wanted[h] = this.level(h);
        this.alertsOn = !!c['intraday_alerts'];
        this.proximity = Number(c['stop_proximity_pct'] ?? 1.5);
      },
      error: (e) => this.error.set(errorText(e)),
    });
  }

  toggle(): void {
    this.busy.set(true);
    this.api.killSwitch(!this.killSwitch()).subscribe({
      next: (r) => { this.killSwitch.set(r.kill_switch); this.busy.set(false); },
      error: (e) => { this.error.set(errorText(e)); this.busy.set(false); },
    });
  }

  saveToken(): void { this.api.setToken(this.token.trim()); this.saved.set(true); this.load(); }

  show(v: unknown): string {
    if (v === undefined || v === null) return '–';
    if (typeof v === 'object') {
      return Object.entries(v as Record<string, unknown>)
        .map(([k, x]) => `${k}: ${typeof x === 'object' && x !== null ? (x as any).model ?? JSON.stringify(x) : x}`).join(' · ');
    }
    return String(v);
  }
}

export type { Theme };
