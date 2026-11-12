import { Injectable, signal } from '@angular/core';

export type Theme = 'system' | 'light' | 'dark';
const KEY = 'stock_primer.theme';

/** Stamps data-theme on <html>; "system" removes it so prefers-color-scheme applies. */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  readonly theme = signal<Theme>(this.read());

  constructor() { this.apply(this.theme()); }

  set(t: Theme): void {
    this.theme.set(t);
    try { localStorage.setItem(KEY, t); } catch { /* storage unavailable */ }
    this.apply(t);
  }

  private read(): Theme {
    try { return (localStorage.getItem(KEY) as Theme) || 'system'; } catch { return 'system'; }
  }

  private apply(t: Theme): void {
    const el = document.documentElement;
    t === 'system' ? el.removeAttribute('data-theme') : el.setAttribute('data-theme', t);
  }
}
