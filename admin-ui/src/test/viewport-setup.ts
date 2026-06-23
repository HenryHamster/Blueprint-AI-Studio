// Browser-mode setup file for the viewport regression suite.
//
// Imports `index.css` (which `@import "tailwindcss"` pulls in) so that
// Tailwind utility classes actually apply at test time. Without this,
// the @tailwindcss/vite plugin in vitest.viewport.config.ts is a no-op
// — utility classes like `break-all`, `min-w-0`, `truncate`,
// `overflow-x-auto`, the entire `w-*` family etc. become inert, and the
// gate produces both false positives (production-correct components
// that fail the test) and false negatives (real overflow that Tailwind
// would expose passes).
//
// Sets the React act() flag so `act()` warnings don't pollute test logs when
// the canary tests render via `react-dom/client.createRoot`. We deliberately
// don't import `@testing-library/jest-dom` matchers here — browser mode
// asserts on real layout, not DOM-shape matchers, so they'd be unused weight.
//
// Cast through `Record` to avoid editing the global type definitions for a
// single setup file; `IS_REACT_ACT_ENVIRONMENT` is the documented opt-in
// flag from React's testing API.
import '../index.css';
import { afterEach, beforeEach } from 'vitest';

(globalThis as unknown as Record<string, boolean>).IS_REACT_ACT_ENVIRONMENT = true;

// ---------------------------------------------------------------------------
// Unhandled-rejection guard
//
// The browser-mode viewport job runs all tests in a single browser worker.
// When any component's mount-effect apiFetch escapes without a try/catch, it
// becomes an `unhandledrejection` event on the browser's `window` — the
// individual test reports green (no assertion failed) but the whole job goes
// red with a cryptic worker-level error.  This guard converts that silent
// job-level crash into a legible per-test failure naming the offending event.
//
// Implementation: a module-level listener pushes events into a list; an
// `afterEach` hook drains the list and re-throws the first captured reason.
// Using `beforeEach` to reset ensures a rejection from test N doesn't
// contaminate test N+1.
// ---------------------------------------------------------------------------
const _pendingRejections: Array<{ reason: unknown; type: string }> = [];

window.addEventListener('unhandledrejection', (e: PromiseRejectionEvent) => {
  _pendingRejections.push({ reason: e.reason, type: e.type });
});

beforeEach(() => {
  _pendingRejections.length = 0;
});

afterEach(() => {
  const captured = _pendingRejections.splice(0);
  if (captured.length === 0) return;
  const first = captured[0];
  const msg =
    first.reason instanceof Error
      ? first.reason.message
      : String(first.reason);
  throw new Error(
    `[assertion_type:unhandled_rejection] A component's async effect escaped without a try/catch: ${msg}` +
    (captured.length > 1 ? ` (+ ${captured.length - 1} more rejection(s))` : ''),
  );
});
