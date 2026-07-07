/* eslint-disable react-hooks/refs */
import { useEffect, useRef } from 'react';

/**
 * Shared, coordinated polling.
 *
 * Background
 * ----------
 * The admin UI historically owned 14+ INDEPENDENT `setInterval` pollers, and
 * `DashboardHome` alone fired ~12 parallel admin requests every 30s tick. Each
 * site set up its own timer with its own phase, so identical-cadence pollers
 * drifted apart and their request bursts never lined up. That hammered the
 * single-leader Cloud Run instance (and amplified its OOM pressure) and never
 * paused while the operator's tab sat in the background.
 *
 * What this provides
 * ------------------
 *  1. A SINGLE shared clock per cadence. Every `usePoller(cb, ms)` subscriber
 *     with the same `intervalMs` ticks off one coordinator timer, so their
 *     bursts align into one window instead of scattering. Aligned bursts let
 *     `apiFetch`'s in-flight de-dup actually collapse identical concurrent GETs
 *     (e.g. DashboardHome + CatchUpPanel both hitting `/api/admin/digest`).
 *  2. PAUSE-WHEN-HIDDEN. When the tab is backgrounded (`document.hidden`), the
 *     shared clock stops firing — no polling happens off-screen. When the tab
 *     becomes visible again we fire one immediate catch-up tick and resume, so
 *     the data the operator sees is fresh on return.
 *  3. De-duplication. Request-level de-dup of identical concurrent GETs already
 *     lives in `apiFetch`; coordinating cadences here is what makes that dedup
 *     bite. This hook additionally guarantees a subscriber never has two of its
 *     own ticks in flight at once (a slow callback skips the overlapping tick).
 *
 * Behavior preservation
 * ----------------------
 * For a visible tab this is equivalent to the previous per-site
 * `setInterval(cb, ms)` plus a leading call: the callback runs once on
 * mount/dep-change and then once per `intervalMs` while visible. The ONLY
 * intentional behavioral change is that polling pauses while hidden (and
 * catches up on return) — the data shown is unchanged.
 */

type Tick = () => void;

interface CadenceClock {
  intervalMs: number;
  subscribers: Set<Tick>;
  timer: ReturnType<typeof setInterval> | null;
}

// One clock per distinct cadence, shared process-wide across all subscribers.
const clocks = new Map<number, CadenceClock>();

let visibilityHooked = false;

function isHidden(): boolean {
  // `document.hidden` is undefined in non-browser environments; treat absence
  // as "visible" so the hook still polls (e.g. SSR / unusual test envs).
  return typeof document !== 'undefined' && document.hidden === true;
}

function fireClock(clock: CadenceClock): void {
  // Snapshot so a subscriber that unsubscribes mid-iteration can't mutate the
  // set we're iterating.
  for (const tick of [...clock.subscribers]) {
    try {
      tick();
    } catch {
      /* a single subscriber throwing must not kill the shared clock */
    }
  }
}

function startTimer(clock: CadenceClock): void {
  if (clock.timer != null || isHidden() || clock.subscribers.size === 0) return;
  clock.timer = setInterval(() => {
    // Defensive: if the tab went hidden between scheduling and firing, skip.
    if (isHidden()) return;
    fireClock(clock);
  }, clock.intervalMs);
}

function stopTimer(clock: CadenceClock): void {
  if (clock.timer != null) {
    clearInterval(clock.timer);
    clock.timer = null;
  }
}

function onVisibilityChange(): void {
  if (isHidden()) {
    // Tab backgrounded — freeze every cadence.
    for (const clock of clocks.values()) stopTimer(clock);
    return;
  }
  // Tab foregrounded — fire one immediate catch-up tick per cadence so the
  // operator sees fresh data on return, then resume the shared clocks.
  for (const clock of clocks.values()) {
    if (clock.subscribers.size > 0) fireClock(clock);
    startTimer(clock);
  }
}

function ensureVisibilityHook(): void {
  if (visibilityHooked || typeof document === 'undefined') return;
  document.addEventListener('visibilitychange', onVisibilityChange);
  visibilityHooked = true;
}

function subscribe(intervalMs: number, tick: Tick): () => void {
  ensureVisibilityHook();
  let clock = clocks.get(intervalMs);
  if (!clock) {
    clock = { intervalMs, subscribers: new Set(), timer: null };
    clocks.set(intervalMs, clock);
  }
  clock.subscribers.add(tick);
  startTimer(clock);

  return () => {
    const c = clocks.get(intervalMs);
    if (!c) return;
    c.subscribers.delete(tick);
    if (c.subscribers.size === 0) {
      stopTimer(c);
      clocks.delete(intervalMs);
    }
  };
}

/** Default shared cadence — matches the historical 30s dashboard poll. */
export const DEFAULT_POLL_MS = 30_000;

export interface UsePollerOptions {
  /** Poll cadence in ms. Subscribers sharing a cadence tick on one clock. */
  intervalMs?: number;
  /** When false, the poller is fully disabled (no leading call, no polling). */
  enabled?: boolean;
  /**
   * When true (default) the callback fires once immediately on mount and on
   * every dependency change, mirroring the old `load(); setInterval(load)`
   * pattern. Set false to only run on shared ticks.
   */
  leading?: boolean;
  /**
   * Extra dependencies that, when changed, tear down and re-establish the
   * poller — triggering a fresh leading call. This mirrors the old per-site
   * `useEffect([...deps])` exactly: pass the same inputs your callback closes
   * over (e.g. `[selectedProjectId, windowHours]`). The `callback` identity
   * itself does NOT drive resubscription (its latest value is read via a ref),
   * so a re-render that merely recreates the callback won't double-fire.
   */
  deps?: ReadonlyArray<unknown>;
}

/**
 * Run `callback` on a shared, visibility-aware poll clock.
 *
 * `callback` receives an `AbortSignal` that fires when the poller is torn down
 * (unmount / dep change / disable) or when a newer tick supersedes a still
 * in-flight one — wire it into `apiFetch(url, opts, retries, timeout, signal)`
 * to cancel stale requests.
 *
 * @param callback   Async work to run each tick. Re-create via `useCallback`
 *                   with the inputs it closes over (the poller re-subscribes
 *                   whenever its identity changes), exactly like a `useEffect`
 *                   dependency.
 */
export function usePoller(
  callback: (signal: AbortSignal) => void | Promise<void>,
  options: UsePollerOptions = {},
): void {
  const { intervalMs = DEFAULT_POLL_MS, enabled = true, leading = true, deps = [] } = options;

  // Keep the latest callback in a ref so a re-render that produces a new
  // callback identity does NOT force a resubscribe/leading-call on its own;
  // resubscription is driven only by the effect deps below.
  const callbackRef = useRef(callback);
  callbackRef.current = callback;

  useEffect(() => {
    if (!enabled) return;

    let disposed = false;
    // Guards against a subscriber overlapping itself: a slow callback that is
    // still in flight when the next tick arrives skips that tick.
    let inFlight = false;
    let currentController: AbortController | null = null;

    const run = () => {
      if (disposed || inFlight) return;
      inFlight = true;
      const controller = new AbortController();
      currentController = controller;
      Promise.resolve(callbackRef.current(controller.signal))
        .catch(() => { /* callback owns its own error handling */ })
        .finally(() => {
          inFlight = false;
          if (currentController === controller) currentController = null;
        });
    };

    if (leading) run();
    const unsubscribe = subscribe(intervalMs, run);

    return () => {
      disposed = true;
      unsubscribe();
      currentController?.abort();
    };
    // `callback` is intentionally excluded — its latest value is read via the
    // ref. Resubscription (and the leading call) is driven by cadence/enabled/
    // leading plus the caller-supplied `deps`, mirroring the previous per-site
    // `useEffect([...deps])` exactly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, enabled, leading, ...deps]);
}

// Exposed for tests only — lets a test reset shared module state and inspect
// whether any clock is actively timing.
export const __pollerInternals = {
  reset(): void {
    for (const clock of clocks.values()) stopTimer(clock);
    clocks.clear();
  },
  hasActiveTimers(): boolean {
    for (const clock of clocks.values()) {
      if (clock.timer != null) return true;
    }
    return false;
  },
  subscriberCount(intervalMs: number): number {
    return clocks.get(intervalMs)?.subscribers.size ?? 0;
  },
};
