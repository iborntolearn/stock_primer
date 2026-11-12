import { Component, computed, input, signal } from '@angular/core';

import { istDate } from '../core/format';

export interface LineSeries { key: string; label: string; slot: number | null; points: { date: string; value: number }[]; }

/**
 * Rebased lines on one axis (all start at 100). Colour comes from a fixed slot per entity (never from rank);
 * slot null = reference series, drawn dashed in muted ink. Legend always; end labels when <= 4 series;
 * crosshair tooltip; table view.
 */
@Component({
  selector: 'app-line-chart',
  template: `
    <figure class="viz">
      <figcaption class="row spread">
        <span class="row legend">@for (s of series(); track s.key) {
          <span class="key"><span class="sw" [class.ref]="s.slot === null" [style.background]="color(s)"></span>{{ s.label }}</span>
        }</span>
        <button type="button" class="ghost small" (click)="showTable.set(!showTable())">{{ showTable() ? 'Chart' : 'Table' }}</button>
      </figcaption>
      @if (dates().length < 2) {
        <p class="muted">Not enough history to draw.</p>
      } @else if (showTable()) {
        <div class="table-wrap scroll"><table>
          <thead><tr><th>Date</th>@for (s of series(); track s.key) { <th class="num">{{ s.label }}</th> }</tr></thead>
          <tbody>@for (d of dates(); track d) {
            <tr><td>{{ fmtDate(d) }}</td>@for (s of series(); track s.key) { <td class="num">{{ fmt(lookup().get(s.key)?.get(d)) }}</td> }</tr>
          }</tbody></table></div>
      } @else {
        <svg [attr.viewBox]="'0 0 ' + W + ' ' + H" preserveAspectRatio="none" role="img" [attr.aria-label]="ariaLabel()"
             (pointermove)="move($event)" (pointerleave)="hover.set(null)">
          @for (t of yTicks(); track t.v) {
            <line class="grid" [class.base]="t.v === 100" [attr.x1]="PAD_L" [attr.x2]="W - PAD_R" [attr.y1]="t.y" [attr.y2]="t.y" />
            <text class="tick" [attr.x]="PAD_L - 6" [attr.y]="t.y + 4" text-anchor="end">{{ t.v }}</text>
          }
          <text class="tick" [attr.x]="PAD_L" [attr.y]="H - 6">{{ fmtDate(dates()[0]) }}</text>
          <text class="tick" [attr.x]="W - PAD_R" [attr.y]="H - 6" text-anchor="end">{{ fmtDate(dates()[dates().length - 1]) }}</text>
          @for (s of drawn(); track s.key) {
            <path class="line" [class.ref]="s.ref" [attr.d]="s.d" [style.stroke]="s.color" />
          }
          @if (hover(); as i) {
            <line class="cross" [attr.x1]="x(i)" [attr.x2]="x(i)" [attr.y1]="PAD_T" [attr.y2]="H - PAD_B" />
          }
        </svg>
        @if (series().length <= 4) {
          <div class="ends">@for (s of labelled(); track s.key) {
            <span class="end small" [style.top.%]="(s.labelY / H) * 100">{{ s.label }} {{ fmt(s.endV) }}</span>
          }</div>
        }
        @if (hover(); as i) {
          <div class="tip card small" [style.left.%]="(x(i) / W) * 100">
            <strong>{{ fmtDate(dates()[i]) }}</strong>
            @for (s of series(); track s.key) {
              <div><span class="sw" [class.ref]="s.slot === null" [style.background]="color(s)"></span>{{ s.label }} {{ fmt(lookup().get(s.key)?.get(dates()[i])) }}</div>
            }
          </div>
        }
      }
    </figure>`,
  styles: `
    .viz { margin: 0; position: relative; }
    svg { width: calc(100% - 110px); height: 260px; display: block; touch-action: none; }
    .legend { gap: 14px; flex-wrap: wrap; }
    .key { display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 13px; }
    .sw { display: inline-block; width: 12px; height: 3px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
    .sw.ref { background: repeating-linear-gradient(90deg, var(--text-muted) 0 3px, transparent 3px 5px) !important; }
    .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
    .line.ref { stroke-dasharray: 4 3; stroke-width: 1.5; }
    .grid { stroke: var(--grid); stroke-width: 1; vector-effect: non-scaling-stroke; }
    .grid.base { stroke: var(--axis); }
    .cross { stroke: var(--axis); stroke-dasharray: 3 3; vector-effect: non-scaling-stroke; }
    .tick { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
    .ends { position: absolute; right: 0; top: 34px; bottom: 0; width: 106px; }
    .end { position: absolute; left: 4px; transform: translateY(-50%); color: var(--text-secondary); white-space: nowrap; font-variant-numeric: tabular-nums; }
    .tip { position: absolute; top: 36px; transform: translateX(-50%); pointer-events: none; padding: 6px 10px; white-space: nowrap; z-index: 2; }
    .scroll { max-height: 300px; overflow-y: auto; }
  `,
})
export class LineChart {
  series = input.required<LineSeries[]>();
  ariaLabel = input('Rebased price comparison');
  readonly W = 640; readonly H = 260; readonly PAD_L = 44; readonly PAD_R = 8; readonly PAD_T = 12; readonly PAD_B = 28;
  hover = signal<number | null>(null);
  showTable = signal(false);

  dates = computed(() => [...new Set(this.series().flatMap((s) => s.points.map((p) => p.date)))].sort());
  lookup = computed(() => new Map(this.series().map((s) => [s.key, new Map(s.points.map((p) => [p.date, p.value]))])));
  private range = computed(() => {
    const vals = this.series().flatMap((s) => s.points.map((p) => p.value));
    let lo = Math.min(100, ...vals), hi = Math.max(100, ...vals);
    const pad = (hi - lo) * 0.08 || 5;
    return { lo: lo - pad, hi: hi + pad };
  });

  color(s: LineSeries): string { return s.slot === null ? 'var(--text-muted)' : `var(--series-${s.slot})`; }
  x(i: number): number { return this.PAD_L + (i / Math.max(this.dates().length - 1, 1)) * (this.W - this.PAD_L - this.PAD_R); }
  y(v: number): number { const { lo, hi } = this.range(); return this.PAD_T + (1 - (v - lo) / (hi - lo)) * (this.H - this.PAD_T - this.PAD_B); }

  drawn = computed(() => {
    const idx = new Map(this.dates().map((d, i) => [d, i]));
    return this.series().map((s) => {
      const pts = s.points.map((p) => ({ x: this.x(idx.get(p.date)!), y: this.y(p.value) }));
      const last = s.points[s.points.length - 1];
      return { key: s.key, label: s.label, ref: s.slot === null, color: this.color(s),
               d: pts.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(''),
               endY: last ? this.y(last.value) : 0, endV: last?.value };
    });
  });

  /** End-label positions pushed apart so neighbours never overlap (min gap in viewBox units). */
  labelled = computed(() => {
    const gap = 20;
    const rows = this.drawn().map((d) => ({ ...d, labelY: d.endY })).sort((a, b) => a.labelY - b.labelY);
    for (let i = 1; i < rows.length; i++) rows[i].labelY = Math.max(rows[i].labelY, rows[i - 1].labelY + gap);
    const overflow = rows.length ? rows[rows.length - 1].labelY - (this.H - this.PAD_B) : 0;
    if (overflow > 0) for (const r of rows) r.labelY -= overflow;
    return rows;
  });

  yTicks = computed(() => {
    const { lo, hi } = this.range();
    const step = [5, 10, 20, 25, 50, 100].find((s) => (hi - lo) / s <= 6) ?? 100;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push({ v, y: this.y(v) });
    return out;
  });

  fmt(v: number | undefined | null): string { return v === undefined || v === null ? '–' : v.toFixed(1); }
  fmtDate(d: string): string { return istDate(d); }

  move(ev: PointerEvent): void {
    const r = (ev.currentTarget as SVGSVGElement).getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * this.W;
    const n = this.dates().length;
    const i = Math.round(((px - this.PAD_L) / (this.W - this.PAD_L - this.PAD_R)) * (n - 1));
    this.hover.set(Math.max(0, Math.min(n - 1, i)));
  }
}
