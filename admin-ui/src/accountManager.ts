import { initializeApp, deleteApp, getApps, type FirebaseApp } from 'firebase/app';
import {
  getAuth,
  onAuthStateChanged,
  signInWithPopup,
  signOut as firebaseSignOut,
  type Auth,
  type User,
} from 'firebase/auth';
import {
  firebaseConfig,
  hasFirebaseConfig,
  hasDashboardToken,
  dashboardToken,
  authInitError,
  firebaseUnavailableMessage,
  googleProvider,
  readableAuthError,
} from './firebaseConfig';

/**
 * Multi-account manager.
 *
 * Each signed-in Google account is backed by its OWN named Firebase app
 * (`initializeApp(config, id)`), which Firebase persists independently in
 * IndexedDB keyed by app name. That gives true per-account token isolation:
 * account A's ID token never mixes with account B's, because they live in
 * separate Auth instances. An "active account" pointer (persisted in
 * localStorage) selects which Auth instance drives `getIdToken()`,
 * `onAuthChange()`, and the exported `auth` binding — so every API request and
 * every piece of email-keyed cached state re-scopes to the active account.
 *
 * Account transitions (add / switch / sign-out) mutate the persisted registry
 * and then reload the page. The reload is deliberate: it is the simplest sound
 * way to guarantee NO cross-account cache bleed across the app's many
 * hand-rolled pollers, websocket tickets, and email-keyed localStorage — a
 * fresh page load re-scopes everything to the new active account, and because
 * the Firebase session for each account persists, the switch requires no new
 * Google OAuth round-trip (unlike the old full logout + re-login).
 */

export interface StoredAccount {
  /** Stable slot id; doubles as the Firebase app name. */
  id: string;
  email: string | null;
  displayName: string | null;
  uid: string | null;
}

const ACCOUNTS_KEY = 'bp.auth.accounts';
const ACTIVE_KEY = 'bp.auth.activeAccountId';
const DASHBOARD_ACCOUNT_ID = 'dashboard';

const authInstances = new Map<string, Auth>();
let accounts: StoredAccount[] = [];
let activeId: string | null = null;

function readLocalStorage(key: string): string | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null;
  } catch {
    return null;
  }
}

function writeLocalStorage(key: string, value: string | null): void {
  try {
    if (typeof localStorage === 'undefined') return;
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode / SSR) — in-memory state still holds */
  }
}

function loadPersisted(): void {
  const raw = readLocalStorage(ACCOUNTS_KEY);
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      accounts = Array.isArray(parsed)
        ? parsed.filter((a): a is StoredAccount => Boolean(a) && typeof a.id === 'string')
        : [];
    } catch {
      accounts = [];
    }
  } else {
    accounts = [];
  }
  activeId = readLocalStorage(ACTIVE_KEY);
  // Repair a dangling / missing active pointer so the app always has a
  // well-defined active account when any account exists.
  if (activeId && !accounts.some((a) => a.id === activeId)) activeId = null;
  if (!activeId && accounts.length > 0) activeId = accounts[0].id;
}

function persistAccounts(): void {
  writeLocalStorage(ACCOUNTS_KEY, JSON.stringify(accounts));
}

function setActive(id: string | null): void {
  activeId = id;
  writeLocalStorage(ACTIVE_KEY, id);
}

/** Lazily create (or reuse) the named Firebase app + Auth for an account id. */
function ensureAuthInstance(id: string): Auth | null {
  if (!hasFirebaseConfig) return null;
  const existing = authInstances.get(id);
  if (existing) return existing;
  try {
    const existingApp = getApps().find((app) => app.name === id);
    const app: FirebaseApp = existingApp ?? initializeApp(firebaseConfig, id);
    const instance = getAuth(app);
    authInstances.set(id, instance);
    return instance;
  } catch {
    return null;
  }
}

function activeAuth(): Auth | null {
  if (!activeId) return null;
  return ensureAuthInstance(activeId);
}

async function safeDeleteApp(id: string): Promise<void> {
  try {
    const app = getApps().find((a) => a.name === id);
    if (app) await deleteApp(app);
  } catch {
    /* deletion is best-effort */
  }
  authInstances.delete(id);
}

function generateAccountId(): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (uuid) return `acct_${uuid}`;
  } catch {
    /* fall through */
  }
  return `acct_${Date.now()}_${Math.floor(Math.random() * 1e9)}`;
}

function reload(): void {
  try {
    window.location.reload();
  } catch {
    /* no-op outside a browser (tests) */
  }
}

// -- Module init ------------------------------------------------------------
// Rehydrate the registry and instantiate each account's Firebase app so their
// persisted sessions restore. Skipped entirely in dashboard-token mode (a
// single synthetic account) and when Firebase is unconfigured.
if (!hasDashboardToken && hasFirebaseConfig) {
  loadPersisted();
  for (const acct of accounts) ensureAuthInstance(acct.id);
}

export const auth: Auth = activeAuth() ?? ({ currentUser: null } as Auth);

// -- Public API -------------------------------------------------------------

/** List signed-in accounts (dashboard mode reports its single synthetic account). */
export function getAccounts(): StoredAccount[] {
  if (hasDashboardToken) {
    return [
      {
        id: DASHBOARD_ACCOUNT_ID,
        email: 'dashboard-agent@system.local',
        displayName: 'Dashboard agent',
        uid: null,
      },
    ];
  }
  return [...accounts];
}

export function getActiveAccountId(): string | null {
  if (hasDashboardToken) return DASHBOARD_ACCOUNT_ID;
  return activeId;
}

/** ID token for the ACTIVE account only — never another account's. */
export async function getIdToken(): Promise<string> {
  if (hasDashboardToken && dashboardToken) return dashboardToken;
  const instance = activeAuth();
  if (!instance) throw new Error(authInitError ?? firebaseUnavailableMessage);
  const user = instance.currentUser;
  if (!user) throw new Error('Not authenticated');
  return user.getIdToken();
}

/**
 * Get the active account token after Firebase has restored its persisted auth
 * state. This is for best-effort startup work: normal API calls should still
 * fail promptly when the user is signed out.
 */
export async function getIdTokenAfterAuthRestore(timeoutMs = 30_000): Promise<string> {
  if (hasDashboardToken && dashboardToken) return dashboardToken;
  const instance = activeAuth();
  if (!instance) throw new Error(authInitError ?? firebaseUnavailableMessage);

  await new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Authentication restore timed out')), timeoutMs);
    instance.authStateReady().then(
      () => {
        clearTimeout(timeout);
        resolve();
      },
      error => {
        clearTimeout(timeout);
        reject(error);
      },
    );
  });
  return getIdToken();
}

/** Subscribe to the active account's Firebase auth state. */
export function onAuthChange(
  callback: (user: User | null) => void,
  onError?: (message: string) => void,
) {
  if (hasDashboardToken) {
    const handle = setTimeout(
      () => callback({ email: 'dashboard-agent@system.local' } as unknown as User),
      0,
    );
    return () => clearTimeout(handle);
  }
  const instance = activeAuth();
  if (!instance) {
    // Configured-but-no-active-account (logged out) → emit null so the login
    // screen renders. Truly unconfigured → also surface the config error.
    const handle = setTimeout(() => {
      if (!hasFirebaseConfig) onError?.(authInitError ?? firebaseUnavailableMessage);
      callback(null);
    }, 0);
    return () => clearTimeout(handle);
  }
  return onAuthStateChanged(instance, callback, (error) => {
    onError?.(readableAuthError(error));
    callback(null);
  });
}

/**
 * Add a new account via Google sign-in and make it active. Used for both the
 * first sign-in (login screen) and "Add account" from the account menu. On
 * success the page reloads so the new account becomes the active scope.
 * If the signed-in Google user is already registered, the duplicate app is
 * discarded and the existing account is activated instead.
 */
export async function addAccount(): Promise<void> {
  if (hasDashboardToken) return;
  if (!hasFirebaseConfig) throw new Error(authInitError ?? firebaseUnavailableMessage);

  const id = generateAccountId();
  const instance = ensureAuthInstance(id);
  if (!instance) throw new Error(authInitError ?? firebaseUnavailableMessage);

  try {
    const credential = await signInWithPopup(instance, googleProvider);
    const user = credential.user;
    const entry: StoredAccount = {
      id,
      email: user.email,
      displayName: user.displayName,
      uid: user.uid,
    };

    const duplicateIdx = accounts.findIndex((a) => a.uid && user.uid && a.uid === user.uid);
    if (duplicateIdx >= 0) {
      // Same Google identity already tracked — replace the old slot with this
      // freshly-authenticated one (its session is guaranteed live) so we never
      // hold two apps for one user or activate a stale session.
      await safeDeleteApp(accounts[duplicateIdx].id);
      accounts = accounts.map((a, i) => (i === duplicateIdx ? entry : a));
    } else {
      accounts = [...accounts, entry];
    }
    persistAccounts();
    setActive(id);
    reload();
  } catch (error) {
    // Roll back the half-created app so a failed/cancelled popup leaves no
    // orphan Firebase app or dangling registry entry.
    await safeDeleteApp(id);
    throw new Error(readableAuthError(error));
  }
}

/** Back-compat alias for the login-screen button. */
export const signInWithGoogle = addAccount;

/** Switch the active account. No-op if already active or unknown id. */
export function switchAccount(id: string): void {
  if (hasDashboardToken) return;
  if (id === activeId) return;
  if (!accounts.some((a) => a.id === id)) return;
  setActive(id);
  reload();
}

/**
 * Sign out the ACTIVE account and remove it from the registry, then activate
 * the next remaining account (or drop to the login screen if none remain).
 * The optional argument is accepted and ignored for back-compat with callers
 * that passed the old `auth` singleton.
 */
export async function signOut(_authArg?: unknown): Promise<void> {
  void _authArg;
  if (hasDashboardToken || !hasFirebaseConfig) return;
  const id = activeId;
  if (!id) return;

  const instance = authInstances.get(id);
  if (instance) {
    try {
      await firebaseSignOut(instance);
    } catch {
      /* proceed with local removal even if the network sign-out fails */
    }
  }
  await safeDeleteApp(id);
  accounts = accounts.filter((a) => a.id !== id);
  persistAccounts();
  setActive(accounts[0]?.id ?? null);
  reload();
}

/** Sign out and forget every account, returning to the login screen. */
export async function signOutAll(): Promise<void> {
  if (hasDashboardToken || !hasFirebaseConfig) return;
  for (const acct of [...accounts]) {
    const instance = authInstances.get(acct.id);
    if (instance) {
      try {
        await firebaseSignOut(instance);
      } catch {
        /* best-effort */
      }
    }
    await safeDeleteApp(acct.id);
  }
  accounts = [];
  persistAccounts();
  setActive(null);
  reload();
}

/**
 * Test-only: reset in-memory state and re-read persisted accounts. Lets unit
 * tests seed localStorage and observe registry transitions deterministically.
 */
export function __resetForTests(): void {
  authInstances.clear();
  accounts = [];
  activeId = null;
  if (!hasDashboardToken && hasFirebaseConfig) {
    loadPersisted();
    for (const acct of accounts) ensureAuthInstance(acct.id);
  }
}
