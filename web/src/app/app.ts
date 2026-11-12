import { Component, inject, signal } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { Api } from './core/api.service';
import { IstPipe } from './core/format';
import { Health } from './core/models';
import { ThemeService } from './core/theme';
import { AddToPortfolio } from './shared/add-to-portfolio';
import { EvidenceDrawer } from './shared/evidence';

/** Shell: navigation, staleness banner, persistent disclaimer, app-wide drawer and dialog. */
@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, IstPipe, EvidenceDrawer, AddToPortfolio],
  template: `
    <div class="shell">
      <nav class="nav" aria-label="Main">
        <a class="brand" routerLink="/">stock_primer</a>
        @for (l of links; track l.path) {
          <a [routerLink]="l.path" routerLinkActive="on" [routerLinkActiveOptions]="{ exact: l.path === '/' }">
            {{ l.label }}@if (l.path === '/inbox' && pending() > 0) { <span class="badge">{{ pending() }}</span> }</a>
        }
      </nav>
      <main>
        @if (health(); as h) {
          @if (h.kill_switch) { <p class="banner bad top">⏸ Kill switch is on: nothing is auto-published.</p> }
          @if (h.last_gate && !h.last_gate.passed) { <p class="banner warn top">⚠ Data quality gate failed for {{ h.last_gate.date | ist: true }} — no new picks from that run.</p> }
          <p class="muted small status">Data as of {{ h.last_price_date | ist: true }}</p>
        } @else if (apiDown()) {
          <p class="banner bad top">Cannot reach the API. Start it with <code>python -m api.main</code> (port 8000).</p>
        }
        <router-outlet />
      </main>
      <footer class="disclaimer">⚠ Decision-support only. Not investment advice. Personal use.</footer>
    </div>
    <app-evidence-drawer />
    <app-add-to-portfolio />`,
  styles: `
    .shell { display: grid; grid-template-columns: 200px 1fr; grid-template-rows: 1fr auto; min-height: 100vh; }
    .nav { grid-row: 1 / 3; background: var(--surface-1); border-right: 1px solid var(--border); padding: 16px 8px; display: flex; flex-direction: column; gap: 2px; }
    .nav a { color: var(--text-secondary); padding: 8px 12px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; }
    .nav a:hover { background: var(--surface-2); text-decoration: none; }
    .nav a.on { background: var(--surface-2); color: var(--text-primary); font-weight: 600; }
    .brand { font-weight: 700; color: var(--text-primary) !important; margin-bottom: 12px; }
    .badge { background: var(--accent); color: var(--accent-ink); border-radius: 999px; font-size: 11px; padding: 0 7px; }
    main { padding: 24px; max-width: 1280px; width: 100%; min-width: 0; }
    .top { margin: 0 0 12px; }
    .status { margin: -8px 0 12px; text-align: right; }
    .disclaimer { grid-column: 2; padding: 12px 24px; font-size: 12px; color: var(--text-muted); border-top: 1px solid var(--border); }
    @media (max-width: 760px) {
      .shell { grid-template-columns: 1fr; }
      .nav { grid-row: auto; flex-direction: row; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--border); padding: 8px; }
      .brand { display: none; }
      .nav a { white-space: nowrap; }
      main { padding: 16px; }
      .disclaimer { grid-column: 1; padding: 12px 16px; }
    }
  `,
})
export class App {
  private api = inject(Api);
  private theme = inject(ThemeService);   // applies the saved theme at startup
  links = [
    { path: '/', label: 'Today' }, { path: '/market', label: 'Market & events' },
    { path: '/recommendations', label: 'Recommendations' },
    { path: '/analyze', label: 'Analyze' }, { path: '/ask', label: 'Ask' },
    { path: '/compare', label: 'Compare' }, { path: '/stress', label: 'What-if' }, { path: '/inbox', label: 'Review inbox' },
    { path: '/portfolios', label: 'Portfolios' }, { path: '/scorecard', label: 'Scorecard' },
    { path: '/runs', label: 'Runs & audit' }, { path: '/settings', label: 'Settings' },
  ];
  health = signal<Health | null>(null);
  apiDown = signal(false);
  pending = signal(0);

  constructor() {
    const refresh = () => {
      this.api.health().subscribe({ next: (h) => { this.health.set(h); this.apiDown.set(false); }, error: () => this.apiDown.set(true) });
      this.api.tasks().subscribe({ next: (t) => this.pending.set(t.items.length), error: () => {} });
    };
    refresh();
    setInterval(refresh, 60_000);
  }
}
