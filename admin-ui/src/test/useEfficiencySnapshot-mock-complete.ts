import { vi } from 'vitest';

export function createUseEfficiencySnapshotMock(overrides: Record<string, unknown> = {}) {
  const base: Record<keyof typeof import('../hooks/useEfficiencySnapshot'), unknown> = {
    useEfficiencySnapshot: vi.fn(),
    fmtEffSecs: vi.fn(),
    fmtEffRate: vi.fn(),
    fmtEffIter: vi.fn(),
  };
  return { ...base, ...overrides };
}
