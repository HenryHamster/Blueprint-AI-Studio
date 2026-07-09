// ── Barrel re-export file ─────────────────────────────────────────────────────
// Splitting api.ts into domain modules preserves the same public API surface.
// Every consumer that imports from './api' (or '../api') continues to resolve
// the same symbols; no import changes needed for barrel consumers.
//
// Domain modules:
//   api/core.ts      — Infrastructure: API_BASE_URL, authHeaders, apiFetch
//   api/tasks.ts     — Task operations and types
//   api/projects.ts  — Project lifecycle, versions, warm pool, acceptance criteria
//   api/strategic.ts — Strategic/research/SR metrics, improvement lanes, outer loop
//   api/messages.ts  — Alerts, notifications, dead letters
//   api/compute.ts   — Compute jobs, knowledge, fleet, evergreen, cost analytics

export * from './api/core';
export * from './api/tasks';
export * from './api/projects';
export * from './api/strategic';
export * from './api/messages';
export * from './api/compute';
