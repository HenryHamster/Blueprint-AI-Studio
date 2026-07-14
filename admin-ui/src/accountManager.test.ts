import { beforeEach, describe, expect, test, vi } from 'vitest';

// Shared mock state for the fake Firebase app/auth registry.
const state = vi.hoisted(() => ({
  apps: new Map<string, { name: string; options: unknown }>(),
  authByName: new Map<string, { app: { name: string }; currentUser: FakeUser | null }>(),
  nextPopupUser: null as FakeUser | null,
}));

interface FakeUser {
  uid: string;
  email: string | null;
  displayName: string | null;
  getIdToken: () => Promise<string>;
}

function makeUser(uid: string, email: string, token: string): FakeUser {
  return { uid, email, displayName: email.split('@')[0], getIdToken: async () => token };
}

vi.mock('firebase/app', () => ({
  initializeApp: (options: unknown, name: string) => {
    const app = { name, options };
    state.apps.set(name, app);
    return app;
  },
  getApps: () => [...state.apps.values()],
  deleteApp: async (app: { name: string }) => {
    state.apps.delete(app.name);
    state.authByName.delete(app.name);
  },
}));

vi.mock('firebase/auth', () => {
  function getAuth(app: { name: string }) {
    let instance = state.authByName.get(app.name);
    if (!instance) {
      instance = { app, currentUser: null };
      state.authByName.set(app.name, instance);
    }
    return instance;
  }
  return {
    getAuth,
    GoogleAuthProvider: class {
      setCustomParameters() {}
    },
    signInWithPopup: async (authInstance: { currentUser: FakeUser | null }) => {
      const user = state.nextPopupUser;
      if (!user) throw new Error('popup-cancelled');
      authInstance.currentUser = user;
      return { user };
    },
    onAuthStateChanged: (
      authInstance: { currentUser: FakeUser | null },
      cb: (u: FakeUser | null) => void,
    ) => {
      cb(authInstance.currentUser ?? null);
      return () => {};
    },
    signOut: async (authInstance: { currentUser: FakeUser | null }) => {
      authInstance.currentUser = null;
    },
  };
});

vi.mock('./firebaseConfig', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./firebaseConfig')>();
  return {
    ...actual,
    hasFirebaseConfig: true,
    hasDashboardToken: false,
    dashboardToken: '',
    authInitError: null,
    firebaseConfig: { apiKey: 'AIzaTESTKEYTESTKEYTESTKEYTEST', authDomain: 'x.firebaseapp.com', projectId: 'x' },
    googleProvider: {},
  };
});

import * as AM from './accountManager';

beforeEach(() => {
  state.apps.clear();
  state.authByName.clear();
  state.nextPopupUser = null;
  localStorage.clear();
  AM.__resetForTests();
});

async function addUser(uid: string, email: string, token: string) {
  state.nextPopupUser = makeUser(uid, email, token);
  await AM.addAccount();
}

describe('accountManager — registration & persistence', () => {
  test('addAccount registers the account, persists it, and makes it active', async () => {
    await addUser('uid-a', 'alice@example.com', 'token-a');
    const list = AM.getAccounts();
    expect(list).toHaveLength(1);
    expect(list[0].email).toBe('alice@example.com');
    expect(AM.getActiveAccountId()).toBe(list[0].id);
    const persisted = JSON.parse(localStorage.getItem('bp.auth.accounts') ?? '[]');
    expect(persisted).toHaveLength(1);
    expect(localStorage.getItem('bp.auth.activeAccountId')).toBe(list[0].id);
  });

  test('adding the same Google identity twice de-duplicates and keeps the fresh session', async () => {
    await addUser('uid-a', 'alice@example.com', 'token-a');
    await addUser('uid-a', 'alice@example.com', 'token-a-refreshed');
    // Still one account for that identity, now serving the refreshed token.
    expect(AM.getAccounts()).toHaveLength(1);
    expect(AM.getAccounts()[0].uid).toBe('uid-a');
    expect(await AM.getIdToken()).toBe('token-a-refreshed');
  });

  test('a cancelled popup leaves no orphan account or app', async () => {
    state.nextPopupUser = null; // signInWithPopup will throw
    await expect(AM.addAccount()).rejects.toThrow();
    expect(AM.getAccounts()).toHaveLength(0);
    expect(state.apps.size).toBe(0);
  });

  test('accounts and the active pointer survive a reload (re-init from storage)', async () => {
    await addUser('uid-a', 'alice@example.com', 'token-a');
    await addUser('uid-b', 'bob@example.com', 'token-b');
    const activeBefore = AM.getActiveAccountId();
    // Simulate the page reload the manager performs on every switch.
    AM.__resetForTests();
    expect(AM.getAccounts().map((a) => a.email).sort()).toEqual([
      'alice@example.com',
      'bob@example.com',
    ]);
    expect(AM.getActiveAccountId()).toBe(activeBefore);
  });
});

describe('accountManager — token isolation', () => {
  test('getIdToken always returns the ACTIVE account token, never another account', async () => {
    await addUser('uid-a', 'alice@example.com', 'token-a');
    const idA = AM.getActiveAccountId();
    await addUser('uid-b', 'bob@example.com', 'token-b');
    const idB = AM.getActiveAccountId();

    // Active is B → token-b.
    expect(idB).not.toBe(idA);
    expect(await AM.getIdToken()).toBe('token-b');

    // Switch back to A → token-a. No bleed from B.
    AM.switchAccount(idA ?? "");
    expect(AM.getActiveAccountId()).toBe(idA);
    expect(await AM.getIdToken()).toBe('token-a');
  });

  test('getIdToken throws when there is no active account', async () => {
    await expect(AM.getIdToken()).rejects.toThrow(/not authenticated|configured|initialize/i);
  });
});

describe('accountManager — sign-out', () => {
  test('signOut removes the active account and activates the next remaining one', async () => {
    await addUser('uid-a', 'alice@example.com', 'token-a');
    const idA = AM.getActiveAccountId();
    await addUser('uid-b', 'bob@example.com', 'token-b');
    expect(AM.getAccounts()).toHaveLength(2);

    await AM.signOut(); // signs out active (B)
    expect(AM.getAccounts()).toHaveLength(1);
    expect(AM.getAccounts()[0].email).toBe('alice@example.com');
    expect(AM.getActiveAccountId()).toBe(idA);
  });

  test('signOutAll clears every account and returns to a logged-out state', async () => {
    await addUser('uid-a', 'alice@example.com', 'token-a');
    await addUser('uid-b', 'bob@example.com', 'token-b');

    await AM.signOutAll();
    expect(AM.getAccounts()).toHaveLength(0);
    expect(AM.getActiveAccountId()).toBeNull();
    expect(localStorage.getItem('bp.auth.activeAccountId')).toBeNull();
    expect(JSON.parse(localStorage.getItem('bp.auth.accounts') ?? '[]')).toHaveLength(0);
  });

  test('switchAccount ignores unknown ids and same-account switches', async () => {
    await addUser('uid-a', 'alice@example.com', 'token-a');
    const idA = AM.getActiveAccountId();
    AM.switchAccount('does-not-exist');
    expect(AM.getActiveAccountId()).toBe(idA);
    AM.switchAccount(idA ?? "");
    expect(AM.getActiveAccountId()).toBe(idA);
  });
});
