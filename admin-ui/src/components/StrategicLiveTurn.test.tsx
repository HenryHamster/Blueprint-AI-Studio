import { afterEach, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, type ReactNode } from 'react';
import { StrategicResidentActivityWidget } from './StrategicLiveTurn';

// Proves the core truthfulness guarantee: the elapsed timer ticks ONLY while a
// command is genuinely executing, and freezes (never ticks) once the session is
// idle or dead — the fix for the counter that used to tick forever after a
// session died without a `done` frame.
describe('StrategicResidentActivityWidget — timer truthfulness', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-01T12:00:00Z'));
  });

  afterEach(() => {
    if (root) {
      const r = root;
      act(() => r.unmount());
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
    vi.useRealTimers();
  });

  function mount(node: ReactNode) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    act(() => r.render(node));
  }

  const text = () =>
    host?.querySelector('[data-testid="strategic-resident-activity"]')?.textContent ?? '';

  test('executing state advances the elapsed timer each second', () => {
    const startMs = Date.now() - 2000; // 2s already elapsed at mount
    mount(
      <StrategicResidentActivityWidget
        state={{ phase: 'executing', label: 'Working', commandText: '$ go', startMs }}
      />,
    );
    expect(text()).toContain('running… (2s)');
    act(() => vi.advanceTimersByTime(3000));
    expect(text()).toContain('running… (5s)');
  });

  test('dead state renders no ticking timer and stays constant as time advances', () => {
    mount(
      <StrategicResidentActivityWidget
        state={{ phase: 'dead', label: 'Session ended', endMs: Date.now() - 30000 }}
      />,
    );
    const before = text();
    expect(before).not.toContain('running…');
    expect(before).toContain('Session ended');
    act(() => vi.advanceTimersByTime(10000));
    // Nothing ticks: identical text after 10s of wall-clock advance.
    expect(text()).toBe(before);
    expect(text()).not.toContain('running…');
  });

  test('idle state renders waiting copy and no ticking timer', () => {
    mount(
      <StrategicResidentActivityWidget
        state={{ phase: 'idle', label: 'Waiting for direction', endMs: Date.now() }}
      />,
    );
    const before = text();
    expect(before).toContain('Waiting for direction');
    expect(before).not.toContain('running…');
    act(() => vi.advanceTimersByTime(10000));
    expect(text()).toBe(before);
  });

  test('durable waiting state renders one compact activity and idle line', () => {
    mount(
      <StrategicResidentActivityWidget
        state={{
          phase: 'idle',
          durablePhase: 'waiting',
          label: 'Waiting',
          detail: 'Watching task completions',
          idleRemainingSeconds: 7140,
          pendingOperatorMessages: 2,
          pendingWatches: 1,
          firedWatches: 1,
        }}
      />,
    );
    expect(host?.querySelector('[data-testid="strategic-resident-activity"]')?.getAttribute('data-phase'))
      .toBe('waiting');
    expect(text()).toContain('Watching task completions');
    expect(text()).toContain('2 messages pending');
    expect(text()).toContain('2 watches');
    expect(text()).toContain('idle 1h 59m left');
    expect(text()).not.toContain('running…');
  });
});
