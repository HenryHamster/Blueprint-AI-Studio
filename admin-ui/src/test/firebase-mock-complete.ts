import { vi } from 'vitest';

export function createFirebaseMock(overrides: Record<string, unknown> = {}) {
  return {
    auth: { currentUser: null, onAuthStateChanged: () => () => {} },
    signInWithGoogle: vi.fn(),
    getIdToken: vi.fn(async () => 'test-token'),
    onAuthChange: vi.fn(() => () => {}),
    signOut: vi.fn(),
    ...overrides,
  };
}
