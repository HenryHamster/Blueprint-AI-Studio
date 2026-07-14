import { GoogleAuthProvider } from 'firebase/auth';

/**
 * Static Firebase configuration and validation, shared by the multi-account
 * manager (accountManager.ts). This module holds NO auth state — it only
 * derives config validity from the build-time Vite env so the account manager
 * can decide whether real Firebase auth is available, whether the local
 * dashboard-token bypass is active, and how to surface config errors.
 */

function normalizeEnvValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function isLikelyFirebaseWebApiKey(apiKey: string): boolean {
  return /^AIza[0-9A-Za-z_-]{20,}$/.test(apiKey);
}

export const firebaseConfig = {
  apiKey: normalizeEnvValue(import.meta.env.VITE_FIREBASE_API_KEY),
  authDomain: normalizeEnvValue(import.meta.env.VITE_FIREBASE_AUTH_DOMAIN),
  projectId: normalizeEnvValue(import.meta.env.VITE_FIREBASE_PROJECT_ID),
};

export const dashboardToken = normalizeEnvValue(import.meta.env.VITE_DASHBOARD_API_TOKEN);
export const hasDashboardToken = Boolean(dashboardToken);

const hasRequiredFirebaseConfig = Boolean(
  firebaseConfig.apiKey && firebaseConfig.authDomain && firebaseConfig.projectId,
);
const hasValidFirebaseApiKey = isLikelyFirebaseWebApiKey(firebaseConfig.apiKey);
export const hasFirebaseConfig = hasRequiredFirebaseConfig && hasValidFirebaseApiKey;

const firebaseMissingConfigMessage =
  'Firebase authentication is not configured. Set VITE_FIREBASE_API_KEY, VITE_FIREBASE_AUTH_DOMAIN, and VITE_FIREBASE_PROJECT_ID, or use VITE_DASHBOARD_API_TOKEN for local dashboard access.';
const firebaseInvalidConfigMessage =
  'Firebase authentication is not configured with a valid web API key. Set a Firebase web API key in VITE_FIREBASE_API_KEY, or use VITE_DASHBOARD_API_TOKEN for local dashboard access.';

export const firebaseUnavailableMessage = !hasRequiredFirebaseConfig
  ? firebaseMissingConfigMessage
  : hasValidFirebaseApiKey
    ? 'Firebase authentication failed to initialize.'
    : firebaseInvalidConfigMessage;

/** Config-level auth error (invalid API key), known statically before any app is created. */
export const authInitError: string | null =
  hasRequiredFirebaseConfig && !hasValidFirebaseApiKey ? firebaseInvalidConfigMessage : null;

export const googleProvider = new GoogleAuthProvider();

export function readableAuthError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (/auth\/invalid-api-key|api key not valid|invalid api key/i.test(message)) {
    return firebaseInvalidConfigMessage;
  }
  return message;
}
