import { Component, computed, input } from '@angular/core';

/** Tiny single-series trend line for stat tiles. Decorative: the tile states the value and change in text. */
@Component({
  selector: 'app-sparkline',
  template: `
    @if (d()) {
      <svg viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true">
        <path [attr.d]="d()" />
        <circle [attr.cx]="last().x" [attr.cy]="last().y" r="2.2" />
      </svg>
    }`,
  styles: `
    svg { width: 100%; height: 28px; display: block; }
    path { fill: none; stroke: var(--series-1); stroke-width: 1.5; vector-effect: non-scaling-stroke; stroke-linejoin: round; }
    circle { fill: var(--series-1); }
  `,
})
export class Sparkline {
  values = input.required<number[]>();
  private pts = computed(() => {
    const v = this.values();
    if (v.length < 2) return [];
    const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
    return v.map((x, i) => ({ x: (i / (v.length - 1)) * 100, y: 26 - ((x - lo) / span) * 24 }));
  });
  d = computed(() => this.pts().map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(''));
  last = computed(() => this.pts()[this.pts().length - 1] ?? { x: 0, y: 0 });
}
