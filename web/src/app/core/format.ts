import { Pipe, PipeTransform } from '@angular/core';

const IST = 'Asia/Kolkata';

/** Indian digit grouping: 1234567.5 -> 12,34,567.50 */
export function inr(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '–';
  return x.toLocaleString('en-IN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function pct(x: number | null | undefined, digits = 2, signed = true): string {
  if (x === null || x === undefined) return '–';
  const s = x.toFixed(digits);
  return `${signed && x > 0 ? '+' : ''}${s}%`;
}

export function istDateTime(iso: string | null | undefined): string {
  if (!iso) return '–';
  return new Date(iso).toLocaleString('en-IN', { timeZone: IST, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) + ' IST';
}

export function istDate(iso: string | null | undefined): string {
  if (!iso) return '–';
  const d = iso.length === 10 ? new Date(iso + 'T00:00:00+05:30') : new Date(iso);
  return d.toLocaleDateString('en-IN', { timeZone: IST, day: '2-digit', month: 'short', year: 'numeric' });
}

@Pipe({ name: 'inr' })
export class InrPipe implements PipeTransform {
  transform(x: number | null | undefined, digits = 2): string { return inr(x, digits); }
}

@Pipe({ name: 'pct' })
export class PctPipe implements PipeTransform {
  transform(x: number | null | undefined, digits = 2, signed = true): string { return pct(x, digits, signed); }
}

@Pipe({ name: 'ist' })
export class IstPipe implements PipeTransform {
  transform(x: string | null | undefined, dateOnly = false): string { return dateOnly ? istDate(x) : istDateTime(x); }
}

/** Random idempotency key per order attempt (stable across retries of the same submit). */
export function idempotencyKey(): string {
  return (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`);
}
