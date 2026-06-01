import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import { usePoller, DEFAULT_POLL_MS, __pollerInternals } from './usePoller';

// jsdom lets us flip `document.hidden` via the property descriptor; it does not
// expose a setter by default, so we redefine it per-test and dispatch the event
// the way a real browser would.
function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get: () => hidden,
  });
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => (hidden ? 'hidden' : 'visible'),
  });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('usePoller', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setHidden(false);
    __pollerInternals.reset();
  });

  afterEach(() => {
    cleanup();
    __pollerInternals.reset();
    vi.useRealTimers();
  });

  // --- Cadence ---

  it('fires a leading call on mount and then once per interval', async () => {
    const cb = vi.fn();
    renderHook(() => usePoller(cb, { intervalMs: 1000 }));

    // Leading call.
    expect(cb).toHaveBeenCalledTimes(1);

    // Flush the leading call's settle microtask so the self-overlap guard
    // clears before the next tick fires.
    await act(async () => { await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); });
    expect(cb).toHaveBeenCalledTimes(2);

    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); });
    expect(cb).toHaveBeenCalledTimes(4);
  });

  it('skips the leading call when leading: false', () => {
    const cb = vi.fn();
    renderHook(() => usePoller(cb, { intervalMs: 1000, leading: false }));

    expect(cb).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1000); });
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('does nothing when disabled and starts no timer', () => {
    const cb = vi.fn();
    renderHook(() => usePoller(cb, { intervalMs: 1000, enabled: false }));

    expect(cb).not.toHaveBeenCalled();
    expect(__pollerInternals.hasActiveTimers()).toBe(false);
    act(() => { vi.advanceTimersByTime(5000); });
    expect(cb).not.toHaveBeenCalled();
  });

  it('stops polling and tears down the shared clock on unmount', () => {
    const cb = vi.fn();
    const { unmount } = renderHook(() => usePoller(cb, { intervalMs: 1000 }));
    expect(cb).toHaveBeenCalledTimes(1);

    unmount();
    expect(__pollerInternals.subscriberCount(1000)).toBe(0);
    expect(__pollerInternals.hasActiveTimers()).toBe(false);

    act(() => { vi.advanceTimersByTime(5000); });
    expect(cb).toHaveBeenCalledTimes(1); // no further ticks
  });

  it('re-subscribes and re-fires the leading call when deps change', () => {
    const cb = vi.fn();
    const { rerender } = renderHook(
      ({ pid }: { pid: string }) => usePoller(cb, { intervalMs: 1000, deps: [pid] }),
      { initialProps: { pid: 'a' } },
    );
    expect(cb).toHaveBeenCalledTimes(1);

    rerender({ pid: 'b' });
    // Dep change tears down + re-establishes -> a fresh leading call.
    expect(cb).toHaveBeenCalledTimes(2);
  });

  // --- Pause when hidden ---

  it('does not poll while the tab is hidden', () => {
    const cb = vi.fn();
    renderHook(() => usePoller(cb, { intervalMs: 1000 }));
    expect(cb).toHaveBeenCalledTimes(1); // leading

    act(() => { setHidden(true); });
    act(() => { vi.advanceTimersByTime(10_000); });
    // No ticks fired while hidden.
    expect(cb).toHaveBeenCalledTimes(1);
    expect(__pollerInternals.hasActiveTimers()).toBe(false);
  });

  it('fires a catch-up tick and resumes when the tab becomes visible again', async () => {
    const cb = vi.fn();
    renderHook(() => usePoller(cb, { intervalMs: 1000 }));
    expect(cb).toHaveBeenCalledTimes(1); // leading
    await act(async () => { await Promise.resolve(); }); // settle leading call

    act(() => { setHidden(true); });
    act(() => { vi.advanceTimersByTime(5000); });
    expect(cb).toHaveBeenCalledTimes(1); // still just the leading call

    act(() => { setHidden(false); });
    // Immediate catch-up on return.
    expect(cb).toHaveBeenCalledTimes(2);

    // ...and the clock resumes.
    await act(async () => { await Promise.resolve(); }); // settle catch-up call
    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); });
    expect(cb).toHaveBeenCalledTimes(3);
  });

  // --- Coordination / de-dupe ---

  it('drives multiple same-cadence subscribers off ONE shared timer', async () => {
    const a = vi.fn();
    const b = vi.fn();
    renderHook(() => usePoller(a, { intervalMs: 1000 }));
    renderHook(() => usePoller(b, { intervalMs: 1000 }));

    expect(__pollerInternals.subscriberCount(1000)).toBe(2);
    // Settle both leading calls so the self-overlap guards clear.
    await act(async () => { await Promise.resolve(); });
    a.mockClear();
    b.mockClear();

    // A single timer advance fires both subscribers on the same tick, so their
    // request bursts align (which is what lets apiFetch's in-flight de-dup
    // collapse identical concurrent GETs).
    await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve(); });
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
  });

  it('does not overlap a subscriber with itself when the callback is slow', async () => {
    let resolveFirst!: () => void;
    const cb = vi.fn(() => new Promise<void>(res => { resolveFirst = res; }));
    renderHook(() => usePoller(cb, { intervalMs: 1000 }));

    // Leading call is in flight (its promise is unresolved).
    expect(cb).toHaveBeenCalledTimes(1);

    // Tick while still in flight -> skipped.
    act(() => { vi.advanceTimersByTime(1000); });
    expect(cb).toHaveBeenCalledTimes(1);

    // Resolve the in-flight call, then the next tick is allowed through.
    await act(async () => { resolveFirst(); await Promise.resolve(); });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(cb).toHaveBeenCalledTimes(2);
  });

  it('passes an AbortSignal that aborts on teardown', () => {
    const signals: AbortSignal[] = [];
    const cb = vi.fn((signal: AbortSignal) => { signals.push(signal); });
    const { unmount } = renderHook(() => usePoller(cb, { intervalMs: 1000 }));

    expect(signals).toHaveLength(1);
    const [captured] = signals;
    expect(captured.aborted).toBe(false);
    unmount();
    expect(captured.aborted).toBe(true);
  });

  it('exports the historical 30s default cadence', () => {
    expect(DEFAULT_POLL_MS).toBe(30_000);
  });
});
