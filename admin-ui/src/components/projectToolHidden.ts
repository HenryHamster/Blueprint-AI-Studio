// Durable "hidden" state for project-declared (manifest) tools.
//
// Manifest tools are discovered from the project repo's `.blueprint/tools.json`
// and have no DB row, so there is nothing server-side to flip an `enabled`
// flag on. To make the per-tool "Hide" control persist across reloads (rather
// than living only in component state and reappearing on every mount) we store
// the set of hidden slugs in localStorage, keyed per project. This mirrors the
// localStorage-backed preference pattern already used by savedViews.ts.

const HIDDEN_KEY_PREFIX = 'project_tools_hidden';

export function hiddenManifestToolsKey(projectId: string): string {
  return `${HIDDEN_KEY_PREFIX}:${projectId || 'default'}`;
}

/** Load the set of hidden manifest tool slugs for a project (empty on any error). */
export function loadHiddenManifestSlugs(projectId: string): Set<string> {
  try {
    const raw = localStorage.getItem(hiddenManifestToolsKey(projectId));
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((s): s is string => typeof s === 'string' && s.length > 0));
  } catch {
    return new Set();
  }
}

/** Persist the set of hidden manifest tool slugs; removes the key when empty. */
export function saveHiddenManifestSlugs(projectId: string, slugs: Set<string>): void {
  try {
    const key = hiddenManifestToolsKey(projectId);
    if (slugs.size === 0) {
      localStorage.removeItem(key);
      return;
    }
    localStorage.setItem(key, JSON.stringify([...slugs]));
  } catch {
    /* storage unavailable / quota — non-fatal, hide reverts to session-only */
  }
}
