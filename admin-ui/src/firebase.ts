/**
 * Back-compat facade over the multi-account manager.
 *
 * Historically this module owned a single Firebase Auth singleton. Auth is now
 * multi-account (see accountManager.ts): each signed-in account has its own
 * named Firebase app, and an active-account pointer selects the scope. These
 * five exports keep every existing caller working — they always resolve to the
 * ACTIVE account. New multi-account UI (add / switch / sign-out-all / list)
 * imports directly from './accountManager'.
 */
export {
  auth,
  getIdToken,
  onAuthChange,
  signInWithGoogle,
  signOut,
} from './accountManager';
