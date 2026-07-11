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
 * Loading and error states are shown inline; failures in the token fetch
 * surface both within the component and via the optional `onError` callback.
 */
export function ProjectToolIframe({ projectId, tool, onError }: ProjectToolIframeProps) {
  const [state, setState] = useState<IframeState>({ status: 'loading' });

  useEffect(() => {
    let cancelled = false;

    async function loadToken() {
      try {
        const { token } = await fetchAssetToken(projectId, tool.slug);
        if (cancelled) return;
        const url = buildToolAssetUrl(projectId, tool.slug, tool.entry, token);
        setState({ status: 'ready', url });
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load tool';
        setState({ status: 'error', message });
        onError?.();
      }
    }

    void loadToken();
    return () => { cancelled = true; };
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
