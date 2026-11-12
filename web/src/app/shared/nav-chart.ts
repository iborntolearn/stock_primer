import { Component, computed, input, signal } from '@angular/core';

import { inr, istDate } from '../core/format';
import { NavPoint } from '../core/models';

interface Pt { x: number; y: number; }

/**
 * NAV vs benchmark, one y-axis (both in rupees from the same starting cash).
 * Two series -> legend + direct end labels; crosshair tooltip on hover; table view for accessibility.
 */
@Component({
  selector: 'app-nav-chart',
  template: `
    <figure class="viz">
      <figcaption class="row spread">
        <span class="row legend">
          <span class="key"><span class="sw s1"></span>{{ label() }}</span>
          @if (hasBench()) { <span class="key"><span class="sw s2"></span>{{ benchLabel() }}</span> }
        </span>
        <button type="button" class="ghost small" (click)="showTable.set(!showTable())">{{ showTable() ? 'Chart' : 'Table' }}</button>
      </figcaption>
      @if (points().length < 2) {
        <p class="muted">Not enough history yet ({{ points().length }} day{{ points().length === 1 ? '' : 's' }}). The chart appears after two end-of-day snapshots.</p>
      } @else if (showTable()) {
        <div class="table-wrap"><table>
          <thead><tr><th>Date</th><th class="num">{{ label() }}</th>@if (hasBench()) {<th class="num">{{ benchLabel() }}</th>}</tr></thead>
          <tbody>@for (p of points(); track p.date) {
            <tr><td>{{ fmtDate(p.date) }}</td><td class="num">{{ fmt(p.nav) }}</td>@if (hasBench()) {<td class="num">{{ fmt(p.benchmark_nav) }}</td>}</tr>
          }</tbody>
        </table></div>
      } @else {
        <svg [attr.viewBox]="'0 0 ' + W + ' ' + H" preserveAspectRatio="none" role="img" [attr.aria-label]="label() + ' NAV over time'"
             (pointermove)="move($event)" (pointerleave)="hover.set(null)">
          @for (t of yTicks(); track t.v) {
            <line class="grid" [attr.x1]="PAD_L" [attr.x2]="W - PAD_R" [attr.y1]="t.y" [attr.y2]="t.y" />
            <text class="tick" [attr.x]="PAD_L - 6" [attr.y]="t.y + 4" text-anchor="end">{{ t.label }}</text>
          }
          <line class="axis" [attr.x1]="PAD_L" [attr.x2]="W - PAD_R" [attr.y1]="H - PAD_B" [attr.y2]="H - PAD_B" />
          <text class="tick" [attr.x]="PAD_L" [attr.y]="H - 6">{{ fmtDate(points()[0].date) }}</text>
          <text class="tick" [attr.x]="W - PAD_R" [attr.y]="H - 6" text-anchor="end">{{ fmtDate(points()[points().length - 1].date) }}</text>
          @if (hasBench()) { <path class="line s2" [attr.d]="path(benchPts())" /> }
          <path class="line s1" [attr.d]="path(navPts())" />
          @if (hover(); as i) {
            <line class="cross" [attr.x1]="navPts()[i].x" [attr.x2]="navPts()[i].x" [attr.y1]="PAD_T" [attr.y2]="H - PAD_B" />
            <circle class="dot s1" [attr.cx]="navPts()[i].x" [attr.cy]="navPts()[i].y" r="4" />
            @if (hasBench() && benchPts()[i]) { <circle class="dot s2" [attr.cx]="benchPts()[i].x" [attr.cy]="benchPts()[i].y" r="4" /> }
          }
        </svg>
        @if (hover(); as i) {
          <div class="tip card small" [style.left.%]="(navPts()[i].x / W) * 100">
            <strong>{{ fmtDate(points()[i].date) }}</strong><br />
            <span class="sw s1"></span>{{ label() }} ₹{{ fmt(points()[i].nav) }}<br />
            @if (hasBench()) { <span class="sw s2"></span>{{ benchLabel() }} ₹{{ fmt(points()[i].benchmark_nav) }} }
          </div>
        }
      }
    </figure>`,
  styles: `
    .viz { margin: 0; position: relative; }
    svg { width: 100%; height: 220px; display: block; touch-action: none; }
    .legend { gap: 16px; }
    .key { display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); }
    .sw { display: inline-block; width: 12px; height: 3px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
    .sw.s1, .line.s1 { background: var(--series-1); stroke: var(--series-1); }
    .sw.s2, .line.s2 { background: var(--series-2); stroke: var(--series-2); }
    .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
    .grid { stroke: var(--grid); stroke-width: 1; vector-effect: non-scaling-stroke; }
    .axis { stroke: var(--axis); stroke-width: 1; vector-effect: non-scaling-stroke; }
    .cross { stroke: var(--axis); stroke-dasharray: 3 3; vector-effect: non-scaling-stroke; }
    .dot { stroke: var(--surface-1); stroke-width: 2; }
    .dot.s1 { fill: var(--series-1); } .dot.s2 { fill: var(--series-2); }
    .tick { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
    .tip { position: absolute; top: 36px; transform: translateX(-50%); pointer-events: none; padding: 6px 10px; white-space: nowrap; z-index: 2; }
  `,
})
export class NavChart {
  points = input.required<NavPoint[]>();
  label = input('Portfolio');
  benchLabel = input('Nifty 500');
  readonly W = 640; readonly H = 220; readonly PAD_L = 64; readonly PAD_R = 12; readonly PAD_T = 12; readonly PAD_B = 28;
  hover = signal<number | null>(null);
  showTable = signal(false);

  hasBench = computed(() => this.points().some((p) => p.benchmark_nav !== null));
  private range = computed(() => {
    const vals = this.points().flatMap((p) => [p.nav, p.benchmark_nav ?? p.nav]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (lo === hi) { lo *= 0.99; hi *= 1.01; }
    const pad = (hi - lo) * 0.08;
    return { lo: lo - pad, hi: hi + pad };
  });
  private xy(i: number, v: number): Pt {
    const n = this.points().length;
    const { lo, hi } = this.range();
    return { x: this.PAD_L + (i / Math.max(n - 1, 1)) * (this.W - this.PAD_L - this.PAD_R),
             y: this.PAD_T + (1 - (v - lo) / (hi - lo)) * (this.H - this.PAD_T - this.PAD_B) };
  }
  navPts = computed(() => this.points().map((p, i) => this.xy(i, p.nav)));
  benchPts = computed(() => this.points().map((p, i) => this.xy(i, p.benchmark_nav ?? p.nav)));
  yTicks = computed(() => {
    const { lo, hi } = this.range();
    return [0, 0.5, 1].map((f) => {
      const v = lo + (hi - lo) * f;
      return { v, y: this.xy(0, v).y, label: inr(v / 1e5, 1) + 'L' };
    });
  });

  path(pts: Pt[]): string { return pts.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(''); }
  fmt(v: number | null): string { return inr(v, 0); }
  fmtDate(d: string): string { return istDate(d); }

  move(ev: PointerEvent): void {
    const svg = ev.currentTarget as SVGSVGElement;
    const r = svg.getBoundingClientRect();
    const x = ((ev.clientX - r.left) / r.width) * this.W;
    const n = this.points().length;
    const i = Math.round(((x - this.PAD_L) / (this.W - this.PAD_L - this.PAD_R)) * (n - 1));
    this.hover.set(Math.max(0, Math.min(n - 1, i)));
  }
}
