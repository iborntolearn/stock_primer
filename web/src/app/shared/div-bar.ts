import { Component, computed, input } from '@angular/core';

/** One diverging bar from a centre baseline: positive right in --div-pos, negative left in --div-neg. */
@Component({
  selector: 'app-div-bar',
  template: `
    <div class="bar" role="img" [attr.aria-label]="label() || (value() + '')">
      <span class="mid"></span>
      @if (value() !== 0) { <span class="fill" [class.neg]="value() < 0" [style.width.%]="width()"></span> }
    </div>`,
  styles: `
    :host { display: block; min-width: 80px; }
    .bar { position: relative; height: 10px; }
    .mid { position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: var(--axis); }
    .fill { position: absolute; left: 50%; height: 100%; background: var(--div-pos); border-radius: 0 4px 4px 0; }
    .fill.neg { left: auto; right: 50%; background: var(--div-neg); border-radius: 4px 0 0 4px; }
  `,
})
export class DivBar {
  value = input.required<number>();
  max = input.required<number>();       // the largest |value| in the set, for a shared scale
  label = input('');
  width = computed(() => (Math.min(Math.abs(this.value()), this.max() || 1) / (this.max() || 1)) * 50);
}
