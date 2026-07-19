import { useState, useEffect, useRef, useCallback, type ReactNode } from 'react';
import { AlertTriangle, ArrowUpRight, BarChart3, FileImage, FileText, FileVideo, Gauge, Maximize2, Network, RefreshCw, Table2, X } from 'lucide-react';
import type {
  StrategicContextArtifactViewablePreview,
  StrategicContextPackageDescriptor,
  StrategicContextPackageDrilldown,
  StrategicContextDescriptorChartPoint,
  StrategicContextRenderableDescriptor,
} from '../strategicStateApi';
import { resolveArtifactDownloadUrl } from '../api';
import { StrategicContextMap } from './StrategicContextMap';
import { EvidenceTierBadge } from './EvidenceTierBadge';

type StrategicContextTarget = StrategicContextPackageDrilldown['target'];

interface StrategicContextArtifactSlotProps {
  contextPackage?: StrategicContextPackageDescriptor | null;
  onOpenTarget?: (target?: StrategicContextTarget | null) => void;
  showHeader?: boolean;
  showMapDescriptor?: boolean;
  projectId?: string | null;
}

function formatCompactNumber(value: number | string | null | undefined): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return String(value ?? '');
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(value ?? 0);
}

function descriptorLabel(kind: string): string {
  if (kind === 'context_map') return 'context map';
  if (kind === 'metric_cards') return 'metrics';
  if (kind === 'pdf') return 'pdf';
  if (kind === 'task_detail') return 'task';
  if (kind === 'direction_detail') return 'direction';
  if (kind === 'message_detail') return 'message';
  if (kind === 'resource_detail') return 'resource';
  return kind.replace(/_/g, ' ');
}

function metricToneClass(tone?: string | null): string {
  if (tone === 'good') return 'text-emerald-300';
  if (tone === 'warning') return 'text-amber-300';
  if (tone === 'critical') return 'text-rose-300';
  if (tone === 'info' || tone === 'active') return 'text-cyan-300';
  return 'text-neutral-100';
}

function statusToneClass(descriptor: StrategicContextRenderableDescriptor): string {
  const normalized = descriptor.status?.toLowerCase() ?? '';
  if (descriptor.unsupported_reason || descriptor.kind === 'unsupported') {
    return 'border-amber-500/25 bg-amber-500/10 text-amber-300';
  }
  if (descriptor.stale || normalized.includes('stale')) {
    return 'border-amber-500/25 bg-amber-500/10 text-amber-300';
  }
  if (descriptor.empty || normalized.includes('empty')) {
    return 'border-neutral-800 bg-neutral-950/50 text-neutral-500';
  }
  if (normalized.includes('failed') || normalized.includes('blocked') || normalized.includes('critical')) {
    return 'border-rose-500/25 bg-rose-500/10 text-rose-300';
  }
  if (normalized.includes('ready') || normalized.includes('complete')) {
    return 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300';
  }
  return 'border-neutral-800 bg-neutral-950/50 text-neutral-400';
}

function descriptorIcon(kind: string): ReactNode {
  if (kind === 'context_map') return <Network className="h-3.5 w-3.5" aria-hidden="true" />;
  if (kind === 'metric_cards') return <Gauge className="h-3.5 w-3.5" aria-hidden="true" />;
  if (kind === 'table') return <Table2 className="h-3.5 w-3.5" aria-hidden="true" />;
  if (kind === 'image') return <FileImage className="h-3.5 w-3.5" aria-hidden="true" />;
  if (kind === 'video') return <FileVideo className="h-3.5 w-3.5" aria-hidden="true" />;
  if (kind === 'pdf') return <FileText className="h-3.5 w-3.5" aria-hidden="true" />;
  if (kind === 'chart' || kind === 'plot') return <BarChart3 className="h-3.5 w-3.5" aria-hidden="true" />;
  if (kind === 'unsupported') return <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />;
  return <FileText className="h-3.5 w-3.5" aria-hidden="true" />;
}

function safeImageUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

type MediaStatus = 'loading' | 'ready' | 'error';

interface FreshMedia {
  /** Best usable URL — the freshly minted signed URL when available, else the baked one. */
  url: string | null;
  status: MediaStatus;
  /** True when a storage_uri + projectId is available to re-mint a fresh signed URL.
   *  False for baked-only media — retry() cannot produce a different URL in that case. */
  canRetry: boolean;
  /** True when a fresh signed URL was successfully re-minted (or the retention window
   *  explicitly lapsed) but the media element still failed to load — indicates the GCS
   *  object was deleted (7-day lifecycle reap) rather than a stale signed URL that a
   *  re-mint would fix. Drives the truthful "media deleted" vs "URL expired" copy. */
  reaped: boolean;
  /** Re-mint the signed URL. Only meaningful when canRetry is true. */
  retry: () => void;
  /** Call from the media element's `onError` when the loaded URL turns out to be dead. */
  onMediaError: () => void;
}

/**
 * Resolve the freshest usable media URL and expose a small state machine so the
 * canvas can render a loading skeleton, an error+retry affordance, and a live
 * preview — instead of a bare (possibly broken) `<img>`.
 *
 * When `projectId` and `storageUri` are present, mint a fresh signed URL from
 * the stable `gs://` ref on mount (and whenever they change, or on `retry()`)
 * so the preview survives baked-URL expiry on a long-idle canvas. Until that
 * resolves — or if resolution fails, or when no ref exists — fall back to the
 * baked URL. `onMediaError()` flips to the error state when even the resolved
 * URL is dead (e.g. an expired signed link), and `retry()` re-mints it.
 */
function useFreshMediaUrl(
  projectId: string | null | undefined,
  storageUri: string | null | undefined,
  revision: string | null | undefined,
  bakedUrl: string | null | undefined,
  expiresAt?: string | null | undefined,
): FreshMedia {
  const generation = storageUri ? (revision ? `${storageUri}@${revision}` : storageUri) : null;
  // Key the resolved URL by the storage generation, so a stale resolution
  // from a previous revision or storage_uri is ignored once it changes (rather than
  // resetting state synchronously inside the effect, which cascades renders).
  const [resolved, setResolved] = useState<{ generation: string; url: string } | null>(null);
  // Records the terminal result of the sign attempt, keyed by the attempt it
  // belongs to. Derived (rather than a synchronous `resolving` flag set inside
  // the effect) so the effect only mutates state from its async callbacks.
  const [outcome, setOutcome] = useState<{ attempt: number; failed: boolean } | null>(null);
  // Set when the rendered media element itself fails to load its URL.
  const [loadError, setLoadError] = useState(false);
  // Set when the artifact's GCS retention window has lapsed (signed URL
  // resolution failure is permanent — no point retrying).  Updated inside
  // the async fetch callback (not during render) to satisfy the purity rule.
  const [permanentlyExpired, setPermanentlyExpired] = useState(false);
  // Bumped by retry() to force the resolver effect to re-run for the same ref.
  const [attempt, setAttempt] = useState(0);
  // Ref holding the generation value for which an automatic error-retry has
  // already been consumed. Compared against the current generation in
  // onMediaError so each distinct media item (new storage_uri or revision) gets
  // exactly one automatic re-resolve before the manual retry button appears.
  // Written only inside a callback — never during render — to satisfy the
  // react-hooks/refs lint rule.
  const autoRetriedForGenerationRef = useRef<string | null>(null);

  const canResolve = Boolean(projectId && storageUri);

  useEffect(() => {
    if (!projectId || !storageUri) return;
    const controller = new AbortController();
    let cancelled = false;
    const thisAttempt = attempt;
    resolveArtifactDownloadUrl(projectId, storageUri, controller.signal)
      .then(url => {
        if (cancelled) return;
        setResolved({ generation: generation!, url });
        // A fresh URL supersedes any prior element-load failure.
        setLoadError(false);
        setOutcome({ attempt: thisAttempt, failed: false });
        // A freshly minted signed URL means the sign endpoint is reachable;
        // reset any prior permanent-expiry flag so retry can be offered again.
        setPermanentlyExpired(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof Error && e.name === 'AbortError') return;
        // Check if the artifact has permanently expired (GCS lifecycle reap).
        // Date.now() is safe here because this is an async callback, not render.
        if (expiresAt && new Date(expiresAt).getTime() < Date.now()) {
          setPermanentlyExpired(true);
        }
        setOutcome({ attempt: thisAttempt, failed: true });
      });
    return () => { cancelled = true; controller.abort(); };
  }, [projectId, storageUri, generation, attempt, expiresAt]);

  const fresh = resolved && generation && resolved.generation === generation ? resolved.url : null;
  const url = safeImageUrl(fresh) ?? safeImageUrl(bakedUrl);

  // The current sign attempt is still in flight until an outcome is recorded
  // for it; a bumped `attempt` (from retry) resets this to "resolving".
  const resolving = canResolve && outcome?.attempt !== attempt;

  let status: MediaStatus;
  if (loadError) {
    status = 'error';
  } else if (url) {
    status = 'ready';
  } else if (resolving) {
    // Still signing — show a skeleton rather than an empty box.
    status = 'loading';
  } else {
    // No usable URL and nothing left to try (no ref, or the sign attempt failed).
    status = 'error';
  }

  const retry = useCallback(() => {
    setLoadError(false);
    setPermanentlyExpired(false);
    if (canResolve) {
      setAttempt(a => a + 1);
    }
  }, [canResolve]);

  const onMediaError = useCallback(() => {
    // When the media element's URL fails (e.g. expired signed URL) and we have
    // a stable storage_uri to re-resolve from, attempt one automatic re-resolve
    // before surfacing the manual retry button — mirroring KnowledgeBrowser.
    if (canResolve && autoRetriedForGenerationRef.current !== generation) {
      autoRetriedForGenerationRef.current = generation;
      setAttempt(a => a + 1);
    } else {
      // Auto-retry consumed or unavailable. Check if the artifact has
      // permanently expired (GCS lifecycle reap) — if so, the retry button
      // should not appear. Date.now() is safe in this async callback.
      if (expiresAt && new Date(expiresAt).getTime() < Date.now()) {
        setPermanentlyExpired(true);
      }
      setLoadError(true);
    }
  }, [canResolve, generation, expiresAt]);

  // After a fresh signed URL is successfully re-minted (auto-retry) but the
  // media element still fails, the underlying GCS object is gone (7-day
  // lifecycle reap) — re-minting another URL would be futile.  Only offer
  // retry when signing itself failed (which may recover) or no load error yet.
  // Additionally, if the backend stamped an explicit artifact_expires_at that
  // is in the past, the GCS object is permanently gone — the retry button
  // should not appear.  `permanentlyExpired` is set inside async callbacks
  // (never during render) so it satisfies all React 19 purity rules.
  const retryWouldHelp = !permanentlyExpired && (!loadError || outcome?.failed === true);
  // The object is gone (not just a stale URL) when re-minting cannot help on
  // re-mintable media: a fresh signed URL was obtained yet the element still
  // failed, or the backend-stamped retention window explicitly lapsed. This
  // drives the truthful "media deleted" copy instead of "URL expired".
  const reaped = canResolve && !retryWouldHelp;
  return { url, status, canRetry: canResolve && retryWouldHelp, reaped, retry, onMediaError };
}

/** Pulsing "LIVE" badge for mid-run viewable previews. Renders a small overlay
 *  chip with a pulsing dot so operators can visually distinguish a still-updating
 *  preview from a terminal one. Uses the same design in all three rendering
 *  surfaces (strategic canvas, compute jobs panel, artifact gallery drawer). */
function LiveBadge() {
  return (
    <span
      className="absolute left-1 top-1 z-10 flex items-center gap-1 rounded bg-red-600/90 px-1.5 py-0.5 text-ui-xs font-semibold text-white shadow-sm"
      data-testid="live-badge"
    >
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white" />
      LIVE
    </span>
  );
}

/** Full-screen overlay for viewing a single image at full size. Esc / backdrop close. */
function MediaLightbox({ url, alt, onClose }: { url: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`${alt} — full size`}
      onClick={onClose}
      className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-black/85 p-4"
      data-testid="strategic-artifact-lightbox"
    >
      <img
        src={url}
        alt={alt}
        onClick={e => e.stopPropagation()}
        className="max-h-[82vh] max-w-full rounded object-contain"
      />
      <div
        className="flex max-w-full items-center gap-3"
        onClick={e => e.stopPropagation()}
      >
        <span className="min-w-0 truncate text-xs text-neutral-300" title={alt}>{alt}</span>
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex min-h-11 shrink-0 items-center gap-1.5 rounded border border-neutral-700 bg-neutral-900 px-3 text-xs font-semibold text-sky-300 transition-colors hover:border-sky-500/50 hover:text-sky-200"
          data-testid="strategic-artifact-lightbox-newtab"
        >
          <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
          Open in new tab
        </a>
        <button
          type="button"
          onClick={onClose}
          className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded border border-neutral-700 bg-neutral-900 text-neutral-300 transition-colors hover:border-neutral-500 hover:text-neutral-100"
          aria-label="Close full-size preview"
          data-testid="strategic-artifact-lightbox-close"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

function MediaSkeleton({ className }: { className: string }) {
  return (
    <div
      className={`${className} animate-pulse rounded bg-neutral-800/70`}
      data-testid="strategic-artifact-loading"
      aria-hidden="true"
    />
  );
}

function MediaExpired({ label, reaped }: { label: string; reaped?: boolean }) {
  return (
    <div
      className="flex w-full items-center justify-center gap-1.5 px-2 py-2 text-xs text-neutral-500"
      data-testid="strategic-artifact-expired"
    >
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      {label} — {reaped ? 'media deleted' : 'URL expired'}
    </div>
  );
}

function MediaRetry({ label, onRetry }: { label: string; onRetry: () => void }) {
  return (
    <button
      type="button"
      onClick={onRetry}
      className="flex min-h-11 w-full items-center justify-center gap-1.5 px-2 py-2 text-xs text-neutral-400 transition-colors hover:text-neutral-100"
      data-testid="strategic-artifact-retry"
    >
      <RefreshCw className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      {label} — retry
    </button>
  );
}

function ResolvedImage({
  projectId,
  storageUri,
  revision,
  bakedUrl,
  alt,
  className,
  onExpand,
  live,
  expiresAt,
}: {
  projectId?: string | null;
  storageUri?: string | null;
  revision?: string | null;
  bakedUrl?: string | null;
  alt: string;
  className: string;
  onExpand: (url: string, alt: string) => void;
  live?: boolean | null;
  expiresAt?: string | null;
}) {
  const { url, status, canRetry, reaped, retry, onMediaError } = useFreshMediaUrl(projectId, storageUri, revision, bakedUrl, expiresAt);
  if (status === 'loading') return <MediaSkeleton className={className} />;
  if (status === 'error' || !url) {
    return canRetry
      ? <MediaRetry label="Image preview unavailable" onRetry={retry} />
      : <MediaExpired label="Image preview unavailable" reaped={reaped} />;
  }
  return (
    <button
      type="button"
      onClick={() => onExpand(url, alt)}
      title={`Open ${alt} full size`}
      aria-label={`Open ${alt} full size`}
      className="group relative block cursor-zoom-in overflow-hidden rounded"
      data-testid="strategic-artifact-image"
      // The hit area is the rendered image itself (content-sized), so it is
      // exempt from the 44px minimum-touch-target viewport assertion.
      data-viewport-allow-small-target=""
    >
      <img src={url} alt={alt} loading="lazy" onError={onMediaError} className={className} />
      {live && <LiveBadge />}
      <span className="pointer-events-none absolute right-1 top-1 rounded bg-black/60 p-1 text-neutral-200 opacity-0 transition-opacity group-hover:opacity-100">
        <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
    </button>
  );
}

function ResolvedVideo({
  projectId,
  storageUri,
  revision,
  bakedUrl,
  ariaLabel,
  className,
  live,
  expiresAt,
}: {
  projectId?: string | null;
  storageUri?: string | null;
  revision?: string | null;
  bakedUrl?: string | null;
  ariaLabel: string;
  className: string;
  live?: boolean | null;
  expiresAt?: string | null;
}) {
  const { url, status, canRetry, reaped, retry, onMediaError } = useFreshMediaUrl(projectId, storageUri, revision, bakedUrl, expiresAt);
  if (status === 'loading') return <MediaSkeleton className={className} />;
  if (status === 'error' || !url) {
    return canRetry
      ? <MediaRetry label="Video preview unavailable" onRetry={retry} />
      : <MediaExpired label="Video preview unavailable" reaped={reaped} />;
  }
  // Bind the URL directly to <video src> (not a nested <source>) so `onError`
  // fires reliably when the signed URL is dead — matching ComputeJobsPanel.
  const video = (
    <video controls preload="metadata" src={url} onError={onMediaError} className={className} aria-label={ariaLabel} data-testid="strategic-artifact-video">
      Your browser does not support the video element.
    </video>
  );
  if (live) {
    return (
      <div className="relative inline-flex">
        {video}
        <LiveBadge />
      </div>
    );
  }
  return video;
}

function ResolvedPdf({
  projectId,
  storageUri,
  revision,
  bakedUrl,
  label,
  className,
  live,
  expiresAt,
}: {
  projectId?: string | null;
  storageUri?: string | null;
  revision?: string | null;
  bakedUrl?: string | null;
  label: string;
  className: string;
  live?: boolean | null;
  expiresAt?: string | null;
}) {
  // PDFs are never embedded inline on the app origin. Resolve a fresh signed URL
  // (re-minted from storage_uri, falling back to the baked url) and open it in a
  // new tab so the browser renders it in its own sandboxed viewer on the storage
  // origin.
  const { url, status, canRetry, reaped, retry } = useFreshMediaUrl(projectId, storageUri, revision, bakedUrl, expiresAt);
  if (status === 'loading') return <MediaSkeleton className={className} />;
  if (status === 'error' || !url) {
    return canRetry
      ? <MediaRetry label="PDF preview unavailable" onRetry={retry} />
      : <MediaExpired label="PDF preview unavailable" reaped={reaped} />;
  }
  const button = (
    <button
      type="button"
      onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}
      title={`Open ${label} in a new tab`}
      className={className}
      data-testid="strategic-context-pdf-open"
    >
      <FileText className="h-4 w-4 shrink-0 text-sky-400" aria-hidden="true" />
      <span className="min-w-0 break-words">{label}</span>
      <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-500" aria-hidden="true" />
    </button>
  );
  if (live) {
    return (
      <div className="relative">
        {button}
        <LiveBadge />
      </div>
    );
  }
  return button;
}

/**
 * Inline thumbnail strip for an artifact-evidence item's signed
 * `viewable_previews`. The strategic backend already mints per-file signed
 * URLs (and stamps their stable `storage_uri`) onto every artifact-evidence
 * item, but the rail's artifact list only rendered names/refs — this surfaces
 * the image/video previews inline, reusing the same signed-URL re-resolution,
 * skeleton, retry, and lightbox pipeline as the descriptor tiles.
 *
 * Additive and self-hiding: renders nothing when there are no previews with a
 * usable URL or re-resolvable ref, so rows without media are unchanged.
 */
export function ArtifactViewablePreviews({
  previews,
  projectId,
  label,
  expiresAt,
}: {
  previews?: StrategicContextArtifactViewablePreview[] | null;
  projectId?: string | null;
  label?: string | null;
  expiresAt?: string | null;
}) {
  const [lightbox, setLightbox] = useState<{ url: string; alt: string } | null>(null);
  const onExpand = useCallback((url: string, alt: string) => setLightbox({ url, alt }), []);
  const closeLightbox = useCallback(() => setLightbox(null), []);

  // A preview is renderable from EITHER a baked url or a re-resolvable
  // storage_uri — never filter on url alone, or an expired baked url would drop
  // a still-resolvable preview. PDF previews are included alongside images and
  // videos so mixed-media artifacts surface all supported kinds in the strip.
  const items = (previews ?? []).filter(
    preview => (preview.kind === 'image' || preview.kind === 'video' || preview.kind === 'pdf') && (preview.url || preview.storage_uri),
  );
  if (items.length === 0) return null;

  return (
    <div
      className="mt-1 flex gap-1 overflow-x-auto rounded border border-neutral-800 bg-neutral-950/45 p-1 custom-scrollbar"
      data-testid="strategic-artifact-viewable-previews"
    >
      {items.map((preview, i) => {
        const alt = preview.filename || (label ? `${label} ${i + 1}` : `Artifact preview ${i + 1}`);
        if (preview.kind === 'video') {
          return (
            <ResolvedVideo
              key={preview.revision ? `${preview.storage_uri || preview.url}@${preview.revision}` : preview.storage_uri || preview.url || i}
              projectId={projectId}
              storageUri={preview.storage_uri}
              revision={preview.revision}
              bakedUrl={preview.url}
              ariaLabel={alt}
              className="h-20 w-auto flex-shrink-0"
              live={preview.live}
              expiresAt={expiresAt}
            />
          );
        }
        if (preview.kind === 'pdf') {
          return (
            <ResolvedPdf
              key={preview.revision ? `${preview.storage_uri || preview.url}@${preview.revision}` : preview.storage_uri || preview.url || i}
              projectId={projectId}
              storageUri={preview.storage_uri}
              revision={preview.revision}
              bakedUrl={preview.url}
              label={alt}
              className="flex h-20 w-32 flex-shrink-0 flex-col items-center justify-center gap-1 rounded border border-neutral-700 bg-neutral-900 p-1 text-xs text-neutral-300 hover:text-sky-300"
              live={preview.live}
              expiresAt={expiresAt}
            />
          );
        }
        return (
          <ResolvedImage
            key={preview.revision ? `${preview.storage_uri || preview.url}@${preview.revision}` : preview.storage_uri || preview.url || i}
            projectId={projectId}
            storageUri={preview.storage_uri}
            revision={preview.revision}
            bakedUrl={preview.url}
            alt={alt}
            className="h-20 w-auto flex-shrink-0 object-contain"
            onExpand={onExpand}
            live={preview.live}
            expiresAt={expiresAt}
          />
        );
      })}
      {lightbox && <MediaLightbox url={lightbox.url} alt={lightbox.alt} onClose={closeLightbox} />}
    </div>
  );
}

type MediaItem = { url?: string | null; alt?: string | null; filename?: string | null; storage_uri?: string | null; revision?: string | null; live?: boolean | null };

function ImageDescriptorBlock({
  descriptor,
  projectId,
}: {
  descriptor: StrategicContextRenderableDescriptor;
  projectId?: string | null;
}) {
  const [lightbox, setLightbox] = useState<{ url: string; alt: string } | null>(null);
  const onExpand = useCallback((url: string, alt: string) => setLightbox({ url, alt }), []);
  const closeLightbox = useCallback(() => setLightbox(null), []);

  // A preview is renderable if it has EITHER a baked URL or a stable
  // storage_uri we can re-resolve — do not filter on url alone, or an
  // expired baked URL would drop a still-resolvable image.
  const listed = (descriptor.images ?? []).filter(img => img.url || img.storage_uri);
  const fallback = descriptor.image;
  const items: MediaItem[] =
    listed.length > 0
      ? listed
      : fallback && (fallback.url || fallback.storage_uri)
        ? [{ url: fallback.url, alt: fallback.alt, filename: null, storage_uri: fallback.storage_uri, revision: fallback.revision, live: fallback.live }]
        : [];

  return (
    <div className="mt-2 overflow-hidden rounded border border-neutral-800 bg-neutral-950/45" data-testid="strategic-context-image-block">
      {items.length > 1 ? (
        <div className="flex gap-1 overflow-x-auto p-1 custom-scrollbar">
          {items.map((img, i) => (
            <ResolvedImage
              key={img.revision ? `${img.storage_uri || img.url}@${img.revision}` : img.storage_uri || img.url || i}
              projectId={projectId}
              storageUri={img.storage_uri}
              revision={img.revision}
              bakedUrl={img.url}
              alt={img.alt || img.filename || `${descriptor.title} ${i + 1}`}
              className="h-40 w-auto flex-shrink-0 object-contain"
              onExpand={onExpand}
              live={img.live}
              expiresAt={descriptor.artifact_expires_at}
            />
          ))}
        </div>
      ) : items.length === 1 ? (
        <ResolvedImage
          projectId={projectId}
          storageUri={items[0].storage_uri}
          revision={items[0].revision}
          bakedUrl={items[0].url}
          alt={descriptor.image?.alt || items[0].alt || descriptor.title}
          className="max-h-80 w-full object-contain"
          onExpand={onExpand}
          live={items[0].live}
          expiresAt={descriptor.artifact_expires_at}
        />
      ) : (
        <div className="px-2 py-2 text-xs text-neutral-500">
          Image preview unavailable. Open the artifact for the source.
        </div>
      )}
      {lightbox && <MediaLightbox url={lightbox.url} alt={lightbox.alt} onClose={closeLightbox} />}
    </div>
  );
}

function VideoDescriptorBlock({
  descriptor,
  projectId,
}: {
  descriptor: StrategicContextRenderableDescriptor;
  projectId?: string | null;
}) {
  const listed = (descriptor.videos ?? []).filter(v => v.url || v.storage_uri);
  const fallback = descriptor.video;
  const items: MediaItem[] =
    listed.length > 0
      ? listed
      : fallback && (fallback.url || fallback.storage_uri)
        ? [{ url: fallback.url, alt: fallback.alt, filename: null, storage_uri: fallback.storage_uri, revision: fallback.revision, live: fallback.live }]
        : [];

  return (
    <div className="mt-2 overflow-hidden rounded border border-neutral-800 bg-neutral-950/45" data-testid="strategic-context-video-block">
      {items.length > 1 ? (
        <div className="flex gap-1 overflow-x-auto p-1 custom-scrollbar">
          {items.map((v, i) => (
            <ResolvedVideo
              key={v.revision ? `${v.storage_uri || v.url}@${v.revision}` : v.storage_uri || v.url || i}
              projectId={projectId}
              storageUri={v.storage_uri}
              revision={v.revision}
              bakedUrl={v.url}
              ariaLabel={v.alt || v.filename || `${descriptor.title} ${i + 1}`}
              className="h-40 w-auto flex-shrink-0"
              live={v.live}
              expiresAt={descriptor.artifact_expires_at}
            />
          ))}
        </div>
      ) : items.length === 1 ? (
        <ResolvedVideo
          projectId={projectId}
          storageUri={items[0].storage_uri}
          revision={items[0].revision}
          bakedUrl={items[0].url}
          ariaLabel={descriptor.video?.alt || items[0].alt || descriptor.title}
          className="max-h-80 w-full"
          live={items[0].live}
          expiresAt={descriptor.artifact_expires_at}
        />
      ) : (
        <div className="px-2 py-2 text-xs text-neutral-500">
          Video preview unavailable. Open the artifact for the source.
        </div>
      )}
    </div>
  );
}

function MutedStatus({ children }: { children: ReactNode }) {
  return (
    <div className="mt-2 px-1 py-1.5 text-xs text-neutral-500">
      {children}
    </div>
  );
}

function DetailValue({
  children,
  target,
  onOpenTarget,
}: {
  children: ReactNode;
  target?: StrategicContextTarget | null;
  onOpenTarget?: (target?: StrategicContextTarget | null) => void;
}) {
  if (target && onOpenTarget) {
    return (
      <button
        type="button"
        onClick={() => onOpenTarget(target)}
        className="flex min-h-11 min-w-0 items-center justify-between gap-2 rounded border border-neutral-800 bg-neutral-950/45 px-2 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:border-neutral-700 hover:bg-neutral-900"
      >
        <span className="min-w-0 break-words">{children}</span>
        <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
      </button>
    );
  }
  return (
    <div className="min-w-0 break-words rounded border border-neutral-800 bg-neutral-950/35 px-2 py-1.5 text-sm text-neutral-200">
      {children}
    </div>
  );
}

/** A compact SVG polyline sparkline for time-series chart descriptors.
 *
 *  Mirrors the sparkline pattern from ComputeJobsPanel.tsx (80×20 viewBox,
 *  normalized Y range, cyan stroke) adapted for the strategic-canvas
 *  descriptor card.  Points are drawn in order; x-axis is the ordinal index,
 *  y-axis is auto-scaled to the min/max of the values. */
function LineChart({ points }: { points: StrategicContextDescriptorChartPoint[] }) {
  if (points.length === 0) return null;
  // Plot raw values (not Math.abs) so negative, upward-trending metrics —
  // common in RL, e.g. rewards -1.0 → -0.5 → 0.1 — render with the correct
  // trend direction. Mirrors the ComputeJobsPanel.tsx sparkline, which scales
  // raw values.
  const vals = points.map(p => p.value);
  const minVal = Math.min(...vals);
  const maxVal = Math.max(...vals);
  const range = maxVal - minVal || 1;
  const W = 80;
  const H = 20;
  const polyPoints = vals
    .map((v, i) => {
      const x = vals.length === 1 ? W / 2 : (i / (vals.length - 1)) * W;
      const y = H - ((v - minVal) / range) * H;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="w-full overflow-visible" aria-hidden="true">
      <polyline points={polyPoints} fill="none" className="stroke-sky-400" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function DescriptorCard({
  descriptor,
  onOpenTarget,
  projectId,
}: {
  descriptor: StrategicContextRenderableDescriptor;
  onOpenTarget?: (target?: StrategicContextTarget | null) => void;
  projectId?: string | null;
}) {
  const chartPoints = descriptor.chart?.points ?? [];
  const maxChartValue = Math.max(1, ...chartPoints.map(point => Math.abs(point.value)));
  const columns = descriptor.columns ?? [];
  const rows = descriptor.rows ?? [];
  const details = descriptor.details ?? [];
  const canOpen = Boolean(descriptor.target && onOpenTarget);

  return (
    <section
      className="min-w-0 rounded-md border border-workbench-border bg-workbench-card p-2"
      data-testid={`strategic-context-descriptor-${descriptor.kind}`}
    >
      <div className="flex min-w-0 items-start gap-2">
        <span className="mt-0.5 shrink-0 text-neutral-500">{descriptorIcon(descriptor.kind)}</span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="min-w-0 flex-1 break-words text-xs font-semibold text-neutral-100">{descriptor.title}</h3>
            {descriptor.evidence_tier && (
              <span className="shrink-0">
                <EvidenceTierBadge tier={descriptor.evidence_tier} />
              </span>
            )}
            <span className={`shrink-0 rounded border px-1.5 py-0.5 text-ui-sm font-semibold uppercase ${statusToneClass(descriptor)}`}>
              {descriptor.status || descriptorLabel(descriptor.kind)}
            </span>
          </div>
          {descriptor.summary && (
            <p className="mt-1 break-words text-xs leading-relaxed text-neutral-500">{descriptor.summary}</p>
          )}
        </div>
        {canOpen && (
          <button
            type="button"
            onClick={() => onOpenTarget?.(descriptor.target)}
            className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-neutral-900 hover:text-neutral-100"
            title={`Open ${descriptor.title}`}
            aria-label={`Open ${descriptor.title}`}
          >
            <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        )}
      </div>

      {descriptor.unsupported_reason && (
        <div className="mt-2 rounded border border-amber-500/25 bg-amber-500/5 px-2 py-1.5 text-xs leading-relaxed text-amber-200">
          {descriptor.unsupported_reason}
        </div>
      )}

      {descriptor.kind === 'metric_cards' && descriptor.metrics && descriptor.metrics.length > 0 && (
        <div className="mt-2 grid min-w-0 grid-cols-2 gap-1.5">
          {descriptor.metrics.slice(0, 6).map(metric => (
            <button
              key={`${descriptor.id}:${metric.label}`}
              type="button"
              disabled={!metric.target || !onOpenTarget}
              onClick={() => onOpenTarget?.(metric.target)}
              className="min-h-11 min-w-0 rounded border border-neutral-800 bg-neutral-950/45 px-2 py-1.5 text-left disabled:cursor-default"
            >
              <span className="block break-words text-ui-sm font-semibold uppercase text-neutral-500">{metric.label}</span>
              <span className={`mt-0.5 block break-words font-mono text-sm font-semibold ${metricToneClass(metric.tone)}`}>
                {formatCompactNumber(metric.value)}
                {metric.unit ? ` ${metric.unit}` : ''}
              </span>
            </button>
          ))}
        </div>
      )}

      {descriptor.kind === 'table' && columns.length > 0 && (
        <div className="mt-2 overflow-hidden rounded border border-neutral-800">
          <table className="w-full table-fixed text-left text-xs">
            <thead className="bg-neutral-950/70 text-ui-sm uppercase text-neutral-500">
              <tr>
                {columns.slice(0, 4).map(column => (
                  <th key={column.key} className="px-2 py-1.5 font-semibold">{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-800/80">
              {rows.slice(0, 4).map((row, rowIndex) => (
                <tr key={row.id ?? rowIndex} className="text-neutral-300">
                  {columns.slice(0, 4).map(column => (
                    <td key={column.key} className="break-words px-2 py-1.5 align-top">
                      {String(row.cells[column.key] ?? '')}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && (
            <div className="px-2 py-2 text-xs text-neutral-600">No table rows yet.</div>
          )}
        </div>
      )}

      {(descriptor.kind === 'markdown' || descriptor.kind === 'text') && (
        descriptor.body || descriptor.summary ? (
          <pre className="mt-2 max-h-48 min-w-0 overflow-auto whitespace-pre-wrap break-words rounded border border-neutral-800 bg-neutral-950/45 px-2 py-1.5 text-xs leading-relaxed text-neutral-300 custom-scrollbar">
            {descriptor.body || descriptor.summary}
          </pre>
        ) : (
          <MutedStatus>No preview available yet.</MutedStatus>
        )
      )}

      {(descriptor.kind === 'image' || descriptor.image || (descriptor.images?.length ?? 0) > 0) && (
        <ImageDescriptorBlock descriptor={descriptor} projectId={projectId} />
      )}

      {(descriptor.kind === 'video' || descriptor.video || (descriptor.videos?.length ?? 0) > 0) && (
        <VideoDescriptorBlock descriptor={descriptor} projectId={projectId} />
      )}

      {(descriptor.kind === 'pdf' || descriptor.pdf || (descriptor.pdfs?.length ?? 0) > 0) && (() => {
        // A pdf tile is renderable from EITHER a baked url or a re-resolvable
        // storage_uri — never filter on url alone, or an expired baked url would
        // drop a still-resolvable document.
        const listed = (descriptor.pdfs ?? []).filter(d => d.url || d.storage_uri);
        const fallback = descriptor.pdf;
        const items: MediaItem[] =
          listed.length > 0
            ? listed
            : fallback && (fallback.url || fallback.storage_uri)
              ? [{ url: fallback.url, alt: fallback.alt, filename: null, storage_uri: fallback.storage_uri, revision: fallback.revision, live: fallback.live }]
              : [];
        return (
          <div className="mt-2 rounded border border-neutral-800 bg-neutral-950/45 p-1" data-testid="strategic-context-pdf-block">
            {items.length > 0 ? (
              <div className="flex flex-col gap-1">
                {items.map((doc, i) => (
                  <ResolvedPdf
                    key={doc.revision ? `${doc.storage_uri || doc.url}@${doc.revision}` : doc.storage_uri || doc.url || i}
                    projectId={projectId}
                    storageUri={doc.storage_uri}
                    revision={doc.revision}
                    bakedUrl={doc.url}
                    label={doc.alt || doc.filename || (items.length > 1 ? `${descriptor.title} ${i + 1}` : descriptor.title)}
                    className="flex min-h-11 w-full items-center gap-2 rounded border border-neutral-800 bg-neutral-950/45 px-2 py-1.5 text-left text-sm text-neutral-200 transition-colors hover:border-sky-500/50 hover:bg-neutral-900"
                    live={doc.live}
                    expiresAt={descriptor.artifact_expires_at}
                  />
                ))}
              </div>
            ) : (
              <div className="px-2 py-2 text-xs text-neutral-500">
                PDF unavailable. Open the artifact for the source.
              </div>
            )}
          </div>
        );
      })()}

      {/* Omitted-files warning for compute-artifact media previews — mirrors
          ComputeJobsPanel's wording and amber styling. Visible when any files
          were dropped from the preview set (capped at 25 files; 8 MiB/image,
          8 MiB/PDF, 512 MiB/video). */}
      {(descriptor.kind === 'image' || descriptor.kind === 'video' || descriptor.kind === 'pdf' || descriptor.image || descriptor.video || descriptor.pdf || (descriptor.images?.length ?? 0) > 0 || (descriptor.videos?.length ?? 0) > 0 || (descriptor.pdfs?.length ?? 0) > 0) && descriptor.viewable_files_dropped != null && descriptor.viewable_files_dropped > 0 && (
        <div
          className="mt-2 text-ui-xs text-amber-400/90 font-mono"
          data-testid="strategic-artifact-omitted-note"
        >
          {`${descriptor.viewable_files_dropped} file${descriptor.viewable_files_dropped === 1 ? '' : 's'} omitted (preview cap: 25 files · 8 MiB/image · 8 MiB/PDF · 512 MiB/video)`}
        </div>
      )}

      {(descriptor.kind === 'chart' || descriptor.kind === 'plot') && (
        <div className="mt-2 space-y-1.5 rounded border border-neutral-800 bg-neutral-950/45 p-2">
          {descriptor.chart?.kind === 'line' && chartPoints.length > 0 ? (
            <div className="space-y-1">
              {(descriptor.chart?.y_label || descriptor.chart?.x_label) && (
                <div className="flex items-center gap-1.5 text-ui-sm font-mono text-neutral-500">
                  {descriptor.chart.y_label && <span>{descriptor.chart.y_label}</span>}
                  <span className="text-neutral-300">{formatCompactNumber(chartPoints[chartPoints.length - 1].value)}</span>
                  <span className="text-neutral-600">({chartPoints.length} samples)</span>
                </div>
              )}
              <LineChart points={chartPoints} />
            </div>
          ) : chartPoints.length > 0 ? chartPoints.slice(0, 6).map(point => {
            const pct = Math.max(2, Math.round((Math.abs(point.value) / maxChartValue) * 100));
            return (
              <div key={`${descriptor.id}:${point.label}`} className="grid min-w-0 grid-cols-[5.5rem_minmax(0,1fr)_3rem] items-center gap-2 text-xs">
                <span className="min-w-0 break-words text-neutral-500">{point.label}</span>
                <span className="h-2 rounded bg-neutral-800">
                  <span className="block h-2 rounded bg-cyan-500/70" style={{ width: `${pct}%` }} />
                </span>
                <span className="text-right font-mono text-neutral-300">{formatCompactNumber(point.value)}</span>
              </div>
            );
          }) : (
            <div className="text-xs text-neutral-500">No chart points yet.</div>
          )}
        </div>
      )}

      {details.length > 0 && (
        <dl className="mt-2 grid gap-1.5">
          {details.slice(0, 8).map(detail => (
            <div key={`${descriptor.id}:${detail.label}`} className="grid min-w-0 gap-1">
              <dt className="text-ui-sm font-semibold uppercase text-neutral-600">{detail.label}</dt>
              <dd>
                <DetailValue target={detail.target} onOpenTarget={onOpenTarget}>
                  {String(detail.value ?? 'n/a')}
                </DetailValue>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

export function StrategicContextArtifactSlot({
  contextPackage,
  onOpenTarget,
  showHeader = true,
  showMapDescriptor = true,
  projectId,
}: StrategicContextArtifactSlotProps) {
  if (!contextPackage) return null;

  const descriptors = contextPackage.descriptors ?? [];
  const mapDescriptor = descriptors.find(descriptor => descriptor.kind === 'context_map') ?? null;
  const renderableDescriptors = descriptors.filter(descriptor => descriptor.kind !== 'context_map');
  const canvas = contextPackage.canvas ?? null;
  const hasCanvas = Boolean(canvas && (canvas.nodes?.length ?? 0) > 0);
  const shouldShowMapDescriptor = showMapDescriptor && Boolean(mapDescriptor || canvas);
  const hasRenderable = renderableDescriptors.length > 0 || shouldShowMapDescriptor;

  return (
    <div className="mt-2 space-y-2" data-testid="strategic-chat-artifact-slot">
      {showHeader && (
        <div className="flex min-w-0 items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <Network className="h-3.5 w-3.5 shrink-0 text-neutral-500" aria-hidden="true" />
            <span className="truncate text-ui-sm font-semibold uppercase text-neutral-500">Canvas and artifacts</span>
          </div>
          <span className="shrink-0 font-mono text-ui-sm text-neutral-600">
            {formatCompactNumber(renderableDescriptors.length + (canvas ? 1 : 0))}
          </span>
        </div>
      )}

      {!hasRenderable && (
        <div className="px-1 py-1.5 text-xs leading-relaxed text-neutral-500">
          No canvas or artifacts yet.
        </div>
      )}

      {shouldShowMapDescriptor && (
        <section
          className="min-w-0 rounded-md border border-workbench-border bg-workbench-card p-2"
          data-testid="strategic-context-descriptor-context_map"
        >
          <div className="flex min-w-0 items-start gap-2">
            <Network className="mt-0.5 h-3.5 w-3.5 shrink-0 text-neutral-500" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-2">
                <h3 className="min-w-0 flex-1 break-words text-xs font-semibold text-neutral-100">
                  {mapDescriptor?.title ?? canvas?.title ?? 'Strategic context map'}
                </h3>
                <span className={`shrink-0 rounded border px-1.5 py-0.5 text-ui-sm font-semibold uppercase ${mapDescriptor ? statusToneClass(mapDescriptor) : 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300'}`}>
                  {mapDescriptor?.status ?? contextPackage.status ?? 'ready'}
                </span>
              </div>
              <p className="mt-1 break-words text-xs leading-relaxed text-neutral-500">
                {mapDescriptor?.summary
                  ?? `${formatCompactNumber(canvas?.node_count ?? canvas?.nodes?.length ?? 0)} nodes / ${formatCompactNumber(canvas?.edge_count ?? canvas?.edges?.length ?? 0)} links`}
              </p>
            </div>
            {mapDescriptor?.target && onOpenTarget && (
              <button
                type="button"
                onClick={() => onOpenTarget(mapDescriptor.target)}
                className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-neutral-900 hover:text-neutral-100"
                title={`Open ${mapDescriptor.title}`}
                aria-label={`Open ${mapDescriptor.title}`}
              >
                <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            )}
          </div>
          {hasCanvas ? (
            <div className="mt-2 hidden lg:block" data-testid="strategic-chat-canvas-slot">
              <StrategicContextMap canvas={canvas} onOpenTarget={onOpenTarget} />
            </div>
          ) : (
            <div className="mt-2 px-1 py-1.5 text-xs text-neutral-500">
              Map data is not loaded yet.
            </div>
          )}
        </section>
      )}

      {renderableDescriptors.map(descriptor => (
        <DescriptorCard
          key={`${descriptor.kind}:${descriptor.id}`}
          descriptor={descriptor}
          onOpenTarget={onOpenTarget}
          projectId={projectId}
        />
      ))}
    </div>
  );
}
