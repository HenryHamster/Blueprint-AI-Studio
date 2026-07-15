import { vi } from 'vitest';

export function createFirebaseMock(overrides: Record<string, unknown> = {}) {
  const base: Record<keyof typeof import('../firebase'), unknown> = {
    auth: { currentUser: null, onAuthStateChanged: () => () => {} },
    signInWithGoogle: vi.fn(),
    getIdToken: vi.fn(async () => 'test-token'),
    getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
    onAuthChange: vi.fn(() => () => {}),
    signOut: vi.fn(),
  };
  return { ...base, ...overrides };
}
