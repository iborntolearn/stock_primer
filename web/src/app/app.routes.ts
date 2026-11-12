import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', title: 'Today · stock_primer', loadComponent: () => import('./pages/home').then((m) => m.Home) },
  { path: 'recommendations', title: 'Recommendations', loadComponent: () => import('./pages/recommendations').then((m) => m.Recommendations) },
  { path: 'recommendations/:id', title: 'Recommendation', loadComponent: () => import('./pages/reco-detail').then((m) => m.RecoDetail) },
  { path: 'analyze', title: 'Analyze', loadComponent: () => import('./pages/analyze').then((m) => m.Analyze) },
  { path: 'ask', title: 'Ask', loadComponent: () => import('./pages/ask').then((m) => m.Ask) },
  { path: 'compare', title: 'Compare', loadComponent: () => import('./pages/compare').then((m) => m.Compare) },
  { path: 'stress', title: 'What-if', loadComponent: () => import('./pages/stress').then((m) => m.Stress) },
  { path: 'market', title: 'Market & events', loadComponent: () => import('./pages/market').then((m) => m.Market) },
  { path: 'inbox', title: 'Review inbox', loadComponent: () => import('./pages/inbox').then((m) => m.Inbox) },
  { path: 'portfolios', title: 'Portfolios', loadComponent: () => import('./pages/portfolios').then((m) => m.Portfolios) },
  { path: 'portfolios/:id', title: 'Portfolio', loadComponent: () => import('./pages/portfolio-detail').then((m) => m.PortfolioDetail) },
  { path: 'scorecard', title: 'Scorecard', loadComponent: () => import('./pages/scorecard').then((m) => m.Scorecard) },
  { path: 'runs', title: 'Runs', loadComponent: () => import('./pages/runs').then((m) => m.Runs) },
  { path: 'runs/:id', title: 'Run', loadComponent: () => import('./pages/runs').then((m) => m.RunDetail) },
  { path: 'settings', title: 'Settings', loadComponent: () => import('./pages/settings').then((m) => m.Settings) },
  { path: '**', redirectTo: '' },
];
