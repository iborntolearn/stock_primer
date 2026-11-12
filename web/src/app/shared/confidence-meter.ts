import { Component, computed, input } from '@angular/core';

/** Numeric confidence + qualitative label; the "why" (gaps, critic penalty) goes in the tooltip. */
@Component({
  selector: 'app-confidence',
  template: `
    <span class="meter" [title]="why() || label()" role="meter" aria-valuemin="0" aria-valuemax="1" [attr.aria-valuenow]="value()">
      <span class="track"><span class="fill" [style.width.%]="(value() ?? 0) * 100"></span></span>
      <span class="num">{{ value() === null ? '–' : value()!.toFixed(2) }}</span>
      <span class="muted small">{{ label() }}</span>
    </span>`,
  styles: `
    .meter { display: inline-flex; align-items: center; gap: 6px; }
    .track { width: 56px; height: 6px; border-radius: 3px; background: var(--grid); overflow: hidden; }
    .fill { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
  `,
})
export class ConfidenceMeter {
  value = input<number | null>(null);
  why = input<string>('');
  label = computed(() => {
    const v = this.value();
    if (v === null) return '';
    return v >= 0.7 ? 'high' : v >= 0.5 ? 'moderate' : 'low';
  });
}
