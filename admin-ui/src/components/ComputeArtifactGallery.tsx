import { useState, useCallback, useEffect, useRef } from 'react';
import { FileText } from 'lucide-react';
import { resolveArtifactDownloadUrl, type ComputeJob } from '../api';
import { computeJobViewableArtifacts, fmtBytes } from './ComputeJobsPanel.utils';

// Signed download URLs go stale; treat a resolved URL as fresh for slightly
// under the backend's 60-min TTL so a full-size open re-resolves rather than
// navigating to an expired link.
const SIGNED_PREVIEW_FRESH_MS = 50 * 60 * 1000;

type PreviewResolution =
  | { state: 'resolving' }
  | { state: 'resolved'; url: string; resolvedAt: number }
  | { state: 'error' };

const artifactCacheKey = (storageUri: string, revision: string | null | undefined) =>
  revision ? `${storageUri}@${revision}` : storageUri;

/** Self-contained viewable-artifact gallery for a single compute job. Owns its
 *  own signed-preview-URL state (keyed by storage identity plus revision), resolves lazily when
 *  the operator expands the gallery, opens images full-size in a new tab, and
 *  re-resolves on click / media error once a signed URL goes stale.
 *
 *  Used in both the task-detail drawer (testIdPrefix="drawer", the default) and
 *  the compute-jobs panel (testIdPrefix="compute"). Returns null when the job
 *  carries no viewable artifacts and no omission count, so it is safe to drop
 *  into any per-job row. */
export function ComputeArtifactGallery({
  projectId,
  job,
  testIdPrefix = 'drawer',
}: {
  projectId: string | undefined | null;
  job: ComputeJob;
  testIdPrefix?: string;
}) {
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

  // Resolve pending non-PDF artifacts eagerly when the gallery is expanded, and
  // whenever the artifact list changes (e.g. a new revision arrives via polling).
  // Functional setPreviewUrls updates batch correctly so calling resolvePreviewUrl
  // for multiple artifacts in the loop cannot cause an infinite re-render cycle:
  // the first setPreviewUrls sets the key to { state: 'resolving' } (truthy),
  // so the next effect execution skips it via !previewUrls[key].
  useEffect(() => {
    if (!expanded) return;
    const pending = artifacts.filter((art) => {
      if (art.kind === 'pdf') return false;
      const key = artifactCacheKey(art.storage_uri, art.revision);
      return !previewUrls[key];
    });
    if (pending.length === 0) return;
    for (const art of pending) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- resolvePreviewUrl immediately flips state to {state:'resolving'} (truthy), so !previewUrls[key] is false on re-render and the loop exits; cascading re-renders don't occur. setTimeout(0) was removed because it breaks vi.useFakeTimers() in panel tests.
      void resolvePreviewUrl(artifactCacheKey(art.storage_uri, art.revision), art.storage_uri);
    }
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

  const isRunning = job.status === 'RUNNING' || job.status === 'STARTING';
  const liveOm = isRunning ? job.viewable_artifacts_live_omissions : null;
  const liveOmTotal = (liveOm?.size_cap ?? 0) + (liveOm?.count_cap ?? 0);
  const terminalDropped = job.viewable_artifacts_dropped_count ?? 0;
  const omissionCount = isRunning ? liveOmTotal : terminalDropped;

  if (artifacts.length === 0 && omissionCount <= 0) return null;

  function omissionDetail(): string {
    if (liveOm) {
      if ((liveOm.size_cap ?? 0) > 0 && !(liveOm.count_cap ?? 0)) {
        return 'exceeded per-file size limit (8 MiB/image · 8 MiB/PDF · 512 MiB/video)';
      }
      if ((liveOm.count_cap ?? 0) > 0 && !(liveOm.size_cap ?? 0)) {
        return '25-file count limit reached';
      }
    }
    return 'preview cap: 25 files · 8 MiB/image · 8 MiB/PDF · 512 MiB/video';
  }

  const omittedNote =
    omissionCount > 0 ? (
      <div
        className="text-ui-xs text-amber-400/90 font-mono"
        data-testid={`${testIdPrefix}-artifact-omitted-note`}
      >
        {`${omissionCount} file${omissionCount === 1 ? '' : 's'} omitted (${omissionDetail()})`}
      </div>
    ) : null;

  // Every viewable file was dropped by the preview cap — surface the omitted
  // note alone (no toggle, nothing to preview) rather than rendering nothing.
  if (artifacts.length === 0) {
    return (
      <div className="mt-1.5" data-testid={`${testIdPrefix}-artifact-gallery`}>
        {omittedNote}
      </div>
    );
  }

  return (
    <div className="mt-1.5" data-testid={`${testIdPrefix}-artifact-gallery`}>
      <div className="flex items-center gap-1.5 mb-0.5 text-ui-sm text-neutral-500 font-mono">
        <span>Artifacts</span>
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="min-h-[44px] rounded border border-neutral-700 bg-neutral-800 px-1 py-0.5 text-ui-xs text-sky-400 hover:text-sky-300"
          data-testid={`${testIdPrefix}-artifact-gallery-toggle`}
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
            const artLiveBadge = art.live ? (
              <span
                className="absolute left-1 top-1 z-10 flex items-center gap-1 rounded bg-red-600/90 px-1.5 py-0.5 text-ui-xs font-semibold text-white shadow-sm"
                data-testid="live-badge"
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white" />
                LIVE
              </span>
            ) : null;
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
              const pdfBtn = (
                <button
                  type="button"
                  onClick={() => { void openPreviewFullSize(key, art.storage_uri); }}
                  disabled={resolution?.state === 'resolving'}
                  title={`Open ${art.name} in a new tab`}
                  className="flex h-24 w-32 flex-col items-center justify-center gap-1 rounded border border-neutral-800 bg-neutral-950 px-1 text-center text-ui-xs text-neutral-400 hover:border-sky-500/50 disabled:opacity-70"
                  data-testid={`${testIdPrefix}-artifact-pdf`}
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
              );
              return (
                <div key={key} className="flex flex-col gap-0.5" data-testid={`${testIdPrefix}-viewable-artifact`}>
                  {artLiveBadge ? <div className="relative">{pdfBtn}{artLiveBadge}</div> : pdfBtn}
                  {caption}
                </div>
              );
            }
            if (resolution?.state === 'resolved') {
              const mediaElem = art.kind === 'video' ? (
                <video
                  controls
                  preload="metadata"
                  src={resolution.url}
                  onError={() => markPreviewError(key)}
                  className="h-24 max-w-[16rem] rounded border border-neutral-800 bg-neutral-950"
                  data-testid={`${testIdPrefix}-artifact-video`}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => { void openPreviewFullSize(key, art.storage_uri); }}
                  title={`Open ${art.name} full-size in a new tab`}
                  className="rounded border border-neutral-800 bg-neutral-950 hover:border-sky-500/50"
                  data-testid={`${testIdPrefix}-artifact-thumbnail`}
                >
                  <img
                    src={resolution.url}
                    alt={art.name}
                    loading="lazy"
                    onError={() => markPreviewError(key)}
                    className="h-24 w-auto max-w-[16rem] rounded object-contain"
                  />
                </button>
              );
              return (
                <div key={key} className="flex flex-col gap-0.5" data-testid={`${testIdPrefix}-viewable-artifact`}>
                  {artLiveBadge ? <div className="relative">{mediaElem}{artLiveBadge}</div> : mediaElem}
                  {caption}
                </div>
              );
            }
            const placeholderBtn = (
              <button
                type="button"
                onClick={() => { void resolvePreviewUrl(key, art.storage_uri); }}
                disabled={resolution?.state === 'resolving'}
                className="flex h-24 w-32 items-center justify-center rounded border border-neutral-800 bg-neutral-950 px-1 text-center text-ui-xs text-neutral-500 hover:border-sky-500/50 disabled:opacity-70"
                data-testid={`${testIdPrefix}-artifact-placeholder`}
              >
                {resolution?.state === 'resolving'
                  ? 'resolving…'
                  : resolution?.state === 'error'
                    ? 'preview unavailable — retry'
                    : 'load preview'}
              </button>
            );
            return (
              <div key={key} className="flex flex-col gap-0.5" data-testid={`${testIdPrefix}-viewable-artifact`}>
                {artLiveBadge ? <div className="relative">{placeholderBtn}{artLiveBadge}</div> : placeholderBtn}
                {caption}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
