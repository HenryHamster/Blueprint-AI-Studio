import { useState, useCallback, useEffect, useRef } from 'react';
import { FileText } from 'lucide-react';
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

const artifactCacheKey = (storageUri: string, revision: string | null) =>
  revision ? `${storageUri}@${revision}` : storageUri;

/** Self-contained viewable-artifact gallery for a single compute job. Owns its
 *  own signed-preview-URL state (keyed by storage identity plus revision), resolves lazily when
 *  the operator expands the gallery, opens images full-size in a new tab, and
 *  re-resolves on click / media error once a signed URL goes stale.
 *
 *  This is the drawer-friendly extraction of ComputeJobsPanel's inline gallery:
 *  it renders nothing (returns null) when the job carries no viewable artifacts,
 *  so it is safe to drop into any per-job row. */
export function ComputeArtifactGallery({ projectId, job }: { projectId: string; job: ComputeJob }) {
  const [expanded, setExpanded] = useState(false);
  const [previewUrls, setPreviewUrls] = useState<Record<string, PreviewResolution>>({});
  const previewRequestIds = useRef<Record<string, number>>({});

  const artifacts = computeJobViewableArtifacts(job);

  const resolvePreviewUrl = useCallback(
    async (key: string, storageUri: string): Promise<string | null> => {
      if (!projectId) return null;
      const requestId = (previewRequestIds.current[key] ?? 0) + 1;
      previewRequestIds.current[key] = requestId;
      setPreviewUrls((prev) => ({ ...prev, [key]: { state: 'resolving' } }));
      try {
        const url = await resolveArtifactDownloadUrl(projectId, storageUri);
        if (previewRequestIds.current[key] === requestId) {
          setPreviewUrls((prev) => ({ ...prev, [key]: { state: 'resolved', url, resolvedAt: Date.now() } }));
        }
        return url;
      } catch {
        if (previewRequestIds.current[key] === requestId) {
          setPreviewUrls((prev) => ({ ...prev, [key]: { state: 'error' } }));
        }
        return null;
      }
    },
    [projectId],
  );

  const markPreviewError = useCallback((key: string) => {
    setPreviewUrls((prev) => ({ ...prev, [key]: { state: 'error' } }));
  }, []);

  useEffect(() => {
    if (!expanded) return;
    const pending = artifacts.filter((art) => {
      if (art.kind === 'pdf') return false;
      const key = artifactCacheKey(art.storage_uri, art.revision);
      return !previewUrls[key];
    });
    if (pending.length === 0) return;
    const timer = window.setTimeout(() => {
      for (const art of pending) {
        void resolvePreviewUrl(artifactCacheKey(art.storage_uri, art.revision), art.storage_uri);
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [artifacts, expanded, previewUrls, resolvePreviewUrl]);

  const openPreviewFullSize = useCallback(
    async (key: string, storageUri: string) => {
      const cached = previewUrls[key];
      if (cached?.state === 'resolved' && Date.now() - cached.resolvedAt < SIGNED_PREVIEW_FRESH_MS) {
        window.open(cached.url, '_blank', 'noopener,noreferrer');
        return;
      }
      // Stale or errored cache — re-resolve, keeping a pending tab open across
      // the async gap so popup blockers don't eat the navigation.
      const pendingTab = window.open('', '_blank');
      const url = await resolvePreviewUrl(key, storageUri);
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

  const droppedCount = job.viewable_artifacts_dropped_count ?? 0;
  if (artifacts.length === 0 && droppedCount <= 0) return null;

  const omittedNote =
    droppedCount > 0 ? (
      <div
        className="text-ui-xs text-amber-400/90 font-mono"
        data-testid="drawer-artifact-omitted-note"
      >
        {`${droppedCount} file${droppedCount === 1 ? '' : 's'} omitted (exceeds preview cap: 25 files / 8 MiB each)`}
      </div>
    ) : null;

  // Every viewable file was dropped by the preview cap — surface the omitted
  // note alone (no toggle, nothing to preview) rather than rendering nothing.
  if (artifacts.length === 0) {
    return (
      <div className="mt-1.5" data-testid="drawer-artifact-gallery">
        {omittedNote}
      </div>
    );
  }

  return (
    <div className="mt-1.5" data-testid="drawer-artifact-gallery">
      <div className="flex items-center gap-1.5 mb-0.5 text-ui-sm text-neutral-500 font-mono">
        <span>Artifacts</span>
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="min-h-[44px] rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-sky-400 hover:text-sky-300"
          data-testid="drawer-artifact-gallery-toggle"
        >
          {expanded ? 'Hide previews' : `Show previews (${artifacts.length})`}
        </button>
      </div>
      {omittedNote}
      {expanded && (
        <div className="flex flex-wrap gap-2">
          {artifacts.map((art) => {
            const key = artifactCacheKey(art.storage_uri, art.revision);
            const resolution = previewUrls[key];
            const sizeLabel = fmtBytes(art.bytes);
            const caption = (
              <div className="max-w-[10rem] truncate text-ui-xs text-neutral-500 font-mono" title={art.name}>
                {art.name}{sizeLabel ? ` · ${sizeLabel}` : ''}
              </div>
            );
            if (art.kind === 'pdf') {
              // PDFs are never rendered inline on the app origin; they open in
              // the browser's sandboxed viewer on the storage origin.
              // openPreviewFullSize resolves a fresh signed URL and opens it in
              // a new tab.
              return (
                <div key={key} className="flex flex-col gap-0.5" data-testid="drawer-viewable-artifact">
                  <button
                    type="button"
                    onClick={() => { void openPreviewFullSize(key, art.storage_uri); }}
                    disabled={resolution?.state === 'resolving'}
                    title={`Open ${art.name} in a new tab`}
                    className="flex h-24 w-32 flex-col items-center justify-center gap-1 rounded border border-neutral-800 bg-neutral-950 px-1 text-center text-ui-xs text-neutral-400 hover:border-sky-500/50 disabled:opacity-70"
                    data-testid="drawer-artifact-pdf"
                  >
                    <FileText className="h-8 w-8 text-sky-400" />
                    <span>
                      {resolution?.state === 'resolving'
                        ? 'opening…'
                        : resolution?.state === 'error'
                          ? 'open failed — retry'
                          : 'Open PDF'}
                    </span>
                  </button>
                  {caption}
                </div>
              );
            }
            if (resolution?.state === 'resolved') {
              return (
                <div key={key} className="flex flex-col gap-0.5" data-testid="drawer-viewable-artifact">
                  {art.kind === 'video' ? (
                    <video
                      controls
                      preload="metadata"
                      src={resolution.url}
                      onError={() => markPreviewError(key)}
                      className="h-24 max-w-[16rem] rounded border border-neutral-800 bg-neutral-950"
                      data-testid="drawer-artifact-video"
                    />
                  ) : (
                    <button
                      type="button"
                      onClick={() => { void openPreviewFullSize(key, art.storage_uri); }}
                      title={`Open ${art.name} full-size in a new tab`}
                      className="rounded border border-neutral-800 bg-neutral-950 hover:border-sky-500/50"
                      data-testid="drawer-artifact-thumbnail"
                    >
                      <img
                        src={resolution.url}
                        alt={art.name}
                        loading="lazy"
                        onError={() => markPreviewError(key)}
                        className="h-24 w-auto max-w-[16rem] rounded object-contain"
                      />
                    </button>
                  )}
                  {caption}
                </div>
              );
            }
            return (
              <div key={key} className="flex flex-col gap-0.5" data-testid="drawer-viewable-artifact">
                <button
                  type="button"
                  onClick={() => { void resolvePreviewUrl(key, art.storage_uri); }}
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
