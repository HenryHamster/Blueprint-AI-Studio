import { useState, useCallback } from 'react';
import { resolveArtifactDownloadUrl, type ComputeJob } from '../api';
import { computeJobViewableArtifacts, fmtBytes } from './ComputeJobsPanel.utils';

// Signed download URLs go stale; treat a resolved URL as fresh for slightly
// under the backend's 60-min TTL so a full-size open re-resolves rather than
// navigating to an expired link. Mirrors ComputeJobsPanel's inline gallery.
const SIGNED_PREVIEW_FRESH_MS = 50 * 60 * 1000;

type PreviewResolution =
  | { state: 'resolving' }
  | { state: 'resolved'; url: string; resolvedAt: number }
  | { state: 'error' };

/** Self-contained viewable-artifact gallery for a single compute job. Owns its
 *  own signed-preview-URL state (keyed by `storage_uri`), resolves lazily when
 *  the operator expands the gallery, opens images full-size in a new tab, and
 *  re-resolves on click / media error once a signed URL goes stale.
 *
 *  This is the drawer-friendly extraction of ComputeJobsPanel's inline gallery:
 *  it renders nothing (returns null) when the job carries no viewable artifacts,
 *  so it is safe to drop into any per-job row. */
export function ComputeArtifactGallery({ projectId, job }: { projectId: string; job: ComputeJob }) {
  const [expanded, setExpanded] = useState(false);
  const [previewUrls, setPreviewUrls] = useState<Record<string, PreviewResolution>>({});

  const artifacts = computeJobViewableArtifacts(job);

  const resolvePreviewUrl = useCallback(
    async (storageUri: string): Promise<string | null> => {
      if (!projectId) return null;
      setPreviewUrls((prev) => ({ ...prev, [storageUri]: { state: 'resolving' } }));
      try {
        const url = await resolveArtifactDownloadUrl(projectId, storageUri);
        setPreviewUrls((prev) => ({ ...prev, [storageUri]: { state: 'resolved', url, resolvedAt: Date.now() } }));
        return url;
      } catch {
        setPreviewUrls((prev) => ({ ...prev, [storageUri]: { state: 'error' } }));
        return null;
      }
    },
    [projectId],
  );

  const markPreviewError = useCallback((storageUri: string) => {
    setPreviewUrls((prev) => ({ ...prev, [storageUri]: { state: 'error' } }));
  }, []);

  const toggleGallery = useCallback(() => {
    setExpanded((wasExpanded) => {
      if (!wasExpanded) {
        // Lazy resolution: sign URLs only on expand, skipping entries already
        // resolving or resolved.
        for (const art of artifacts) {
          const cached = previewUrls[art.storage_uri];
          if (cached?.state === 'resolving' || cached?.state === 'resolved') continue;
          void resolvePreviewUrl(art.storage_uri);
        }
      }
      return !wasExpanded;
    });
  }, [artifacts, previewUrls, resolvePreviewUrl]);

  const openPreviewFullSize = useCallback(
    async (storageUri: string) => {
      const cached = previewUrls[storageUri];
      if (cached?.state === 'resolved' && Date.now() - cached.resolvedAt < SIGNED_PREVIEW_FRESH_MS) {
        window.open(cached.url, '_blank', 'noopener,noreferrer');
        return;
      }
      // Stale or errored cache — re-resolve, keeping a pending tab open across
      // the async gap so popup blockers don't eat the navigation.
      const pendingTab = window.open('', '_blank');
      const url = await resolvePreviewUrl(storageUri);
      if (!url) {
        pendingTab?.close();
        return;
      }
      if (pendingTab) {
        pendingTab.opener = null;
        pendingTab.location.replace(url);
      } else {
        window.open(url, '_blank', 'noopener,noreferrer');
      }
    },
    [previewUrls, resolvePreviewUrl],
  );

  if (!projectId) return null;
  if (artifacts.length === 0) return null;

  return (
    <div className="mt-1.5" data-testid="drawer-artifact-gallery">
      <div className="flex items-center gap-1.5 mb-0.5 text-ui-sm text-neutral-500 font-mono">
        <span>Artifacts</span>
        <button
          type="button"
          onClick={toggleGallery}
          className="min-h-[44px] rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-sky-400 hover:text-sky-300"
          data-testid="drawer-artifact-gallery-toggle"
        >
          {expanded ? 'Hide previews' : `Show previews (${artifacts.length})`}
        </button>
      </div>
      {expanded && (
        <div className="flex flex-wrap gap-2">
          {artifacts.map((art) => {
            const resolution = previewUrls[art.storage_uri];
            const sizeLabel = fmtBytes(art.bytes);
            const caption = (
              <div className="max-w-[10rem] truncate text-ui-xs text-neutral-500 font-mono" title={art.name}>
                {art.name}{sizeLabel ? ` · ${sizeLabel}` : ''}
              </div>
            );
            if (resolution?.state === 'resolved') {
              return (
                <div key={art.storage_uri} className="flex flex-col gap-0.5" data-testid="drawer-viewable-artifact">
                  {art.kind === 'video' ? (
                    <video
                      controls
                      preload="metadata"
                      src={resolution.url}
                      onError={() => markPreviewError(art.storage_uri)}
                      className="h-24 max-w-[16rem] rounded border border-neutral-800 bg-neutral-950"
                      data-testid="drawer-artifact-video"
                    />
                  ) : (
                    <button
                      type="button"
                      onClick={() => { void openPreviewFullSize(art.storage_uri); }}
                      title={`Open ${art.name} full-size in a new tab`}
                      className="rounded border border-neutral-800 bg-neutral-950 hover:border-sky-500/50"
                      data-testid="drawer-artifact-thumbnail"
                    >
                      <img
                        src={resolution.url}
                        alt={art.name}
                        loading="lazy"
                        onError={() => markPreviewError(art.storage_uri)}
                        className="h-24 w-auto max-w-[16rem] rounded object-contain"
                      />
                    </button>
                  )}
                  {caption}
                </div>
              );
            }
            return (
              <div key={art.storage_uri} className="flex flex-col gap-0.5" data-testid="drawer-viewable-artifact">
                <button
                  type="button"
                  onClick={() => { void resolvePreviewUrl(art.storage_uri); }}
                  disabled={resolution?.state === 'resolving'}
                  className="flex h-24 w-32 items-center justify-center rounded border border-neutral-800 bg-neutral-950 px-1 text-center text-ui-xs text-neutral-500 hover:border-sky-500/50 disabled:opacity-70"
                  data-testid="drawer-artifact-placeholder"
                >
                  {resolution?.state === 'resolving'
                    ? 'resolving…'
                    : resolution?.state === 'error'
                      ? 'preview unavailable — retry'
                      : 'load preview'}
                </button>
                {caption}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
