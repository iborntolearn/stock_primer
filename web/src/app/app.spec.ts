import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { App } from './app';
import { inr, pct } from './core/format';
import { HitlTask } from './core/models';
import { Inbox } from './pages/inbox';
import { Market } from './pages/market';
import { NavChart } from './shared/nav-chart';

describe('format', () => {
  it('uses Indian digit grouping', () => {
    expect(inr(1234567.5)).toBe('12,34,567.50');
    expect(inr(null)).toBe('–');
    expect(pct(2.5)).toBe('+2.50%');
    expect(pct(-1)).toBe('-1.00%');
  });
});

function setup(component: any) {
  TestBed.configureTestingModule({
    imports: [component],
    providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
  });
  return TestBed.inject(HttpTestingController);
}

describe('App shell', () => {
  it('shows the API-down banner and the disclaimer', async () => {
    const http = setup(App);
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    http.expectOne('/api/v1/health').error(new ProgressEvent('error'), { status: 0 });
    http.expectOne((r) => r.url === '/api/v1/hitl/tasks').flush({ items: [] });
    await fixture.whenStable();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Cannot reach the API');
    expect(el.querySelector('.disclaimer')?.textContent).toContain('Not investment advice');
  });

  it('shows the kill-switch banner and pending badge', async () => {
    const http = setup(App);
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    http.expectOne('/api/v1/health').flush({ status: 'ok', last_price_date: '2026-10-12', last_gate: { date: '2026-10-12', passed: true }, kill_switch: true });
    http.expectOne((r) => r.url === '/api/v1/hitl/tasks').flush({ items: [{ id: 1 }, { id: 2 }] });
    await fixture.whenStable();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Kill switch is on');
    expect(el.querySelector('.badge')?.textContent).toContain('2');
  });
});

describe('NavChart', () => {
  it('builds one path per series and a table view', async () => {
    TestBed.configureTestingModule({ imports: [NavChart] });
    const fixture = TestBed.createComponent(NavChart);
    fixture.componentRef.setInput('points', [
      { date: '2026-10-12', nav: 1000000, benchmark_nav: 1000000 },
      { date: '2026-10-13', nav: 1012000, benchmark_nav: 1004000 },
      { date: '2026-10-14', nav: 1008000, benchmark_nav: 1006000 },
    ]);
    await fixture.whenStable();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('path.line').length).toBe(2);
    expect(el.querySelectorAll('.legend .key').length).toBe(2);
    fixture.componentInstance.showTable.set(true);
    await fixture.whenStable();
    expect(el.querySelectorAll('tbody tr').length).toBe(3);
  });

  it('explains when there is too little history', async () => {
    TestBed.configureTestingModule({ imports: [NavChart] });
    const fixture = TestBed.createComponent(NavChart);
    fixture.componentRef.setInput('points', [{ date: '2026-10-12', nav: 1, benchmark_nav: null }]);
    await fixture.whenStable();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Not enough history');
  });
});

describe('Inbox', () => {
  const task: HitlTask = {
    id: 7, task_type: 'approve_reco', subject_type: 'recommendation', subject_id: 3, priority: 2, status: 'pending',
    reason: 'autonomy L1', ai_recommendation: 'approve', created_at: '2026-10-12T13:00:00Z', due_at: '2026-10-12T13:55:00Z',
    default_on_expiry: 'reject', allowed_actions: ['approve', 'edit', 'reject'], decision: null, decided_by: null,
    decided_at: null, decision_note: null,
    payload: { card: { symbol: 'TCS', horizon: 'short', verdict: 'favourable', confidence: 0.6, headline: 'Breakout',
                       levels: { entry_low: 100, entry_high: 102, stop: 95, target: 115 } } },
  };

  it('sends only changed levels on edit', async () => {
    const http = setup(Inbox);
    const fixture = TestBed.createComponent(Inbox);
    fixture.detectChanges();
    http.expectOne((r) => r.url === '/api/v1/hitl/tasks').flush({ items: [task] });
    await fixture.whenStable();
    const cmp = fixture.componentInstance;
    expect(cmp.sel()?.id).toBe(7);
    cmp.editStop = 96;
    cmp.decide(task, 'edit');
    const req = http.expectOne('/api/v1/hitl/tasks/7/decision');
    expect(req.request.body).toEqual({ action: 'edit', note: undefined, edits: { stop: 96 } });
  });
});

describe('Market', () => {
  it('drops empty sectors, signs crores and flags only real LLM inferences', async () => {
    const http = setup(Market);
    const fixture = TestBed.createComponent(Market);
    fixture.detectChanges();
    http.expectOne('/api/v1/market/regime').flush({ as_of: '2026-10-12', regime: 'neutral', score: 0.1, inputs: { fii_5d_cr: -7380.4, unavailable: [] }, assessment: {} });
    http.expectOne((r) => r.url === '/api/v1/market/macro').flush({ series: {}, flows: [] });
    http.expectOne('/api/v1/market/sectors').flush({ as_of: '2026-10-12', sectors: [
      { sector: 'IT', index: 'NIFTY IT', ret_5d_pct: 1, ret_1m_pct: 5, ret_3m_pct: 12, event_overlay: -0.06 },
      { sector: 'Empty', index: 'X', ret_5d_pct: null, ret_1m_pct: null, ret_3m_pct: null, event_overlay: 0 },
      { sector: 'Autos', index: null, ret_5d_pct: null, ret_1m_pct: null, ret_3m_pct: null, event_overlay: -0.28 } ] });
    http.expectOne((r) => r.url === '/api/v1/market/events').flush({ events: [
      { id: 1, event_key: 'k', event_type: 'commodity', title: 'Brent up', summary: null, severity: 4, status: 'active', confidence: 0.7,
        geographies: null, impacts: [{ sector: 'Autos', direction: -1, magnitude: 0.4, horizon: 'short', channel: 'crude', basis: 'approved_map' }] } ] });
    http.expectOne((r) => r.url === '/api/v1/market/calendar').flush({ from: '', to: '', items: [] });
    http.expectOne('/api/v1/market/sensitivity').flush({ rows: [] });
    await fixture.whenStable();
    const cmp = fixture.componentInstance;
    expect(cmp.sectorsSorted().map((r) => r.sector)).toEqual(['Autos', 'IT']);
    expect(cmp.crore(-7380.4)).toBe('-₹7,380 cr');
    expect(cmp.hasInferred()).toBe(false);
    expect(cmp.cell(0, 1)).toBe('transparent');
    expect(cmp.cell(0.6, 0.6)).toContain('var(--div-pos) 55%');
  });
});

describe('splitCitations', () => {
  it('turns [E1] and [E2, E3] into evidence parts', async () => {
    const { splitCitations } = await import('./pages/ask');
    expect(splitCitations('Up 4% [E1]; flows weak [E2, E3].')).toEqual([
      { text: 'Up 4% ' }, { ev: 'E1' }, { text: '; flows weak ' }, { ev: 'E2' }, { ev: 'E3' }, { text: '.' },
    ]);
    expect(splitCitations('no cites')).toEqual([{ text: 'no cites' }]);
  });
});

describe('DivBar', () => {
  it('scales to the shared max and caps at half width', async () => {
    const { DivBar } = await import('./shared/div-bar');
    const f = TestBed.createComponent(DivBar);
    f.componentRef.setInput('value', -5); f.componentRef.setInput('max', 10);
    expect(f.componentInstance.width()).toBe(25);
    f.componentRef.setInput('value', 30);
    expect(f.componentInstance.width()).toBe(50);
  });
});
