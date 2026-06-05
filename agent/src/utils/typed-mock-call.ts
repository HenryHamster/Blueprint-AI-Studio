import { expect, vi } from 'vitest';

// The canonical TypeScript pattern for "accept any callable" uses `any` in the
// function bound so that `Parameters<F>` infers the real parameter tuple from
// the concrete function type passed at each call site. There is no safe
// alternative: `unknown[]` prevents inference and `never[]` inverts variance.
/* eslint-disable @typescript-eslint/no-explicit-any */
type AnyFn = (...args: any[]) => any;

/**
 * Type-safe wrapper around `expect(vi.mocked(fn)).toHaveBeenCalledWith(...)`.
 *
 * Constrains the asserted argument list to `Parameters<F>`, so any future
 * signature change that is not reflected in the assertion becomes a tsc error
 * caught by the existing CI agent job — a type-level safety net that holds even
 * before a test runs. (The Vitest suite itself is now enforced too: `./check`
 * runs `npx vitest run` on agent-touching changes and fails closed, and the
 * `CI / Agent` job runs it as a complementary backstop.)
 *
 * Use for project-internal functions that get refactored (e.g. `setupWorkspace`,
 * `captureTestBaseline`). Leave raw `toHaveBeenCalledWith` in place for
 * Node standard-library spies whose signatures are stable and overloaded.
 */
export function expectCalledWith<F extends AnyFn>(fn: F, ...args: Parameters<F>): void {
  expect(vi.mocked(fn)).toHaveBeenCalledWith(...args);
}

/** Type-safe wrapper around `.toHaveBeenLastCalledWith(...)`. */
export function expectLastCalledWith<F extends AnyFn>(fn: F, ...args: Parameters<F>): void {
  expect(vi.mocked(fn)).toHaveBeenLastCalledWith(...args);
}

/** Type-safe wrapper around `.not.toHaveBeenCalledWith(...)`. */
export function expectNotCalledWith<F extends AnyFn>(fn: F, ...args: Parameters<F>): void {
  expect(vi.mocked(fn)).not.toHaveBeenCalledWith(...args);
}
/* eslint-enable @typescript-eslint/no-explicit-any */
