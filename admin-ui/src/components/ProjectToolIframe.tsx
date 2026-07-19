import { useEffect, useState } from 'react';
import { fetchAssetToken, buildToolAssetUrl, type ProjectToolManifestEntry } from '../api/projectTools';

interface ProjectToolIframeProps {
  projectId: string;
  tool: ProjectToolManifestEntry;
  onError?: () => void;
}

type IframeState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; url: string };

/**
 * Render a project-declared tool in a sandboxed opaque-origin iframe.
 *
 * On mount, fetches a short-lived capability token via `fetchAssetToken`,
 * constructs the asset-serving URL, and renders an `<iframe>` with
 * `sandbox="allow-scripts"` (no `allow-same-origin`) so the tool runs in an
 * opaque origin (`null`) where it cannot access the parent's cookies, DOM,
 * or storage — establishing the security boundary required for serving
 * untrusted third-party web content.
 *
 * The capability token has a short TTL (5 minutes server-side). A tool left
 * open longer than that would otherwise start 401-ing on any asset (re)load
 * once the token in its URL expires, so we proactively re-mint the token a
 * little before it lapses and swap the iframe `src`, keeping the embed valid
 * indefinitely.
 *
 * Loading and error states are shown inline; failures in the token fetch
 * surface both within the component and via the optional `onError` callback.
 */

// Re-mint the token this many ms before its declared expiry so a refresh
// completes before the current token lapses (covers clock skew + fetch time).
const TOKEN_REFRESH_SKEW_MS = 30_000;
// Never schedule a refresh sooner than this, so an already-expired or
// unparseable timestamp cannot spin a tight re-fetch loop.
const MIN_TOKEN_REFRESH_MS = 10_000;
// Fallback cadence when the server omits/garbles `expires_at` (assume the
// documented 5-minute TTL, minus the skew).
const DEFAULT_TOKEN_REFRESH_MS = 5 * 60_000 - TOKEN_REFRESH_SKEW_MS;

export function ProjectToolIframe({ projectId, tool, onError }: ProjectToolIframeProps) {
  const [state, setState] = useState<IframeState>({ status: 'loading' });

  useEffect(() => {
    let cancelled = false;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;

    function scheduleRefresh(expiresAt: string | undefined) {
      const expMs = expiresAt ? Date.parse(expiresAt) : NaN;
      const delay = Number.isFinite(expMs)
        ? Math.max(expMs - Date.now() - TOKEN_REFRESH_SKEW_MS, MIN_TOKEN_REFRESH_MS)
        : DEFAULT_TOKEN_REFRESH_MS;
      refreshTimer = setTimeout(() => { void loadToken(); }, delay);
    }

    async function loadToken() {
      try {
        const { token, expires_at } = await fetchAssetToken(projectId, tool.slug);
        if (cancelled) return;
        const url = buildToolAssetUrl(projectId, tool.slug, tool.entry, token);
        setState({ status: 'ready', url });
        scheduleRefresh(expires_at);
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load tool';
        setState({ status: 'error', message });
        onError?.();
      }
    }

    void loadToken();
    return () => {
      cancelled = true;
      if (refreshTimer !== undefined) clearTimeout(refreshTimer);
    };
  }, [projectId, tool.slug, tool.entry, onError]);

  // ── Loading state ─────────────────────────────────────────────────────────

  if (state.status === 'loading') {
    return (
      <div className="flex items-center justify-center py-16 text-sm text-neutral-500">
        <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-neutral-600 border-t-neutral-300" />
        <span className="ml-2">Loading tool…</span>
      </div>
    );
  }

  // ── Error state ───────────────────────────────────────────────────────────

  if (state.status === 'error') {
    return (
      <div className="rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
        {state.message}
      </div>
    );
  }

  // ── Loaded — render sandboxed iframe ──────────────────────────────────────

  return (
    <iframe
      sandbox="allow-scripts"
      src={state.url}
      title={tool.title}
      className="w-full border-none"
      style={{ minHeight: '420px' }}
      referrerPolicy="no-referrer"
    />
  );
}
