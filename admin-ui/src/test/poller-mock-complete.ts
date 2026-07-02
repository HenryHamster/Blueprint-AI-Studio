import { vi } from 'vitest';

export function createPollerMock(overrides: Record<string, unknown> = {}) {
  return {
    DEFAULT_POLL_MS: 30_000,
    usePoller: vi.fn(),
    __pollerInternals: { reset: vi.fn(), hasActiveTimers: vi.fn(), subscriberCount: vi.fn() },
    ...overrides,
  };
}
