import { initializeApp } from 'firebase/app';
import {
  getAuth,
  signInWithPopup,
  GoogleAuthProvider,
  onAuthStateChanged,
  signOut as firebaseSignOut,
  type Auth,
  type User,
} from 'firebase/auth';

const firebaseConfig = {
  apiKey: normalizeEnvValue(import.meta.env.VITE_FIREBASE_API_KEY),
  authDomain: normalizeEnvValue(import.meta.env.VITE_FIREBASE_AUTH_DOMAIN),
  projectId: normalizeEnvValue(import.meta.env.VITE_FIREBASE_PROJECT_ID),
};

function normalizeEnvValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function isLikelyFirebaseWebApiKey(apiKey: string): boolean {
  return /^AIza[0-9A-Za-z_-]{20,}$/.test(apiKey);
}

const dashboardToken = normalizeEnvValue(import.meta.env.VITE_DASHBOARD_API_TOKEN);
const hasDashboardToken = Boolean(dashboardToken);
const hasRequiredFirebaseConfig = Boolean(firebaseConfig.apiKey && firebaseConfig.authDomain && firebaseConfig.projectId);
const hasValidFirebaseApiKey = isLikelyFirebaseWebApiKey(firebaseConfig.apiKey);
const hasFirebaseConfig = hasRequiredFirebaseConfig && hasValidFirebaseApiKey;
const firebaseMissingConfigMessage = 'Firebase authentication is not configured. Set VITE_FIREBASE_API_KEY, VITE_FIREBASE_AUTH_DOMAIN, and VITE_FIREBASE_PROJECT_ID, or use VITE_DASHBOARD_API_TOKEN for local dashboard access.';
const firebaseInvalidConfigMessage = 'Firebase authentication is not configured with a valid web API key. Set a Firebase web API key in VITE_FIREBASE_API_KEY, or use VITE_DASHBOARD_API_TOKEN for local dashboard access.';
const firebaseUnavailableMessage = !hasRequiredFirebaseConfig
  ? firebaseMissingConfigMessage
  : hasValidFirebaseApiKey
    ? 'Firebase authentication failed to initialize.'
    : firebaseInvalidConfigMessage;

let authInitError: string | null = hasRequiredFirebaseConfig && !hasValidFirebaseApiKey
  ? firebaseInvalidConfigMessage
  : null;
let initializedAuth: Auth | null = null;

if (hasFirebaseConfig) {
  try {
    const app = initializeApp(firebaseConfig);
    initializedAuth = getAuth(app);
  } catch (error) {
    authInitError = error instanceof Error ? error.message : String(error);
  }
}

export const auth: Auth = initializedAuth ?? ({ currentUser: null } as Auth);

const googleProvider = new GoogleAuthProvider();

function readableAuthError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (/auth\/invalid-api-key|api key not valid|invalid api key/i.test(message)) {
    return firebaseInvalidConfigMessage;
  }
  return message;
}

export async function signInWithGoogle() {
  if (!initializedAuth) throw new Error(authInitError ?? firebaseUnavailableMessage);
  try {
    return await signInWithPopup(initializedAuth, googleProvider);
  } catch (error) {
    throw new Error(readableAuthError(error));
  }
}

export async function getIdToken(): Promise<string> {
  if (hasDashboardToken && dashboardToken) {
    return dashboardToken;
  }
  if (!initializedAuth) throw new Error(authInitError ?? firebaseUnavailableMessage);
  const user = initializedAuth.currentUser;
  if (!user) throw new Error('Not authenticated');
  return user.getIdToken();
}

export function onAuthChange(
  callback: (user: User | null) => void,
  onError?: (message: string) => void,
) {
  if (hasDashboardToken) {
    setTimeout(() => callback({ email: 'dashboard-agent@system.local' } as unknown as User), 0);
    return () => {};
  }
  if (!initializedAuth) {
    const handle = setTimeout(() => {
      onError?.(authInitError ?? firebaseUnavailableMessage);
      callback(null);
    }, 0);
    return () => clearTimeout(handle);
  }
  return onAuthStateChanged(
    initializedAuth,
    callback,
    (error) => {
      onError?.(readableAuthError(error));
      callback(null);
    },
  );
}

export async function signOut(_authArg?: Auth | null) {
  if (!initializedAuth) return;
  return firebaseSignOut(initializedAuth);
}
