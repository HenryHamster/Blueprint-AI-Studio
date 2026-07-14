import { useState, useEffect, type ReactNode } from 'react';
import { AlertTriangle, ArrowUpRight, BarChart3, FileImage, FileText, FileVideo, Gauge, Network, Table2 } from 'lucide-react';
import type {
  StrategicContextPackageDescriptor,
  StrategicContextPackageDrilldown,
  StrategicContextRenderableDescriptor,
} from '../strategicStateApi';
import { resolveArtifactDownloadUrl } from '../api';
import { StrategicContextMap } from './StrategicContextMap';

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

/**
 * Resolve the freshest usable media URL. When `projectId` and `storageUri` are
 * present, mint a fresh signed URL from the stable `gs://` ref on mount (and
 * whenever they change) so the preview survives baked-URL expiry on a long-idle
 * canvas. Until that resolves — or if resolution fails, or when no ref exists —
 * fall back to the baked URL. Returns null when nothing is renderable.
 */
function useFreshMediaUrl(
  projectId: string | null | undefined,
  storageUri: string | null | undefined,
  bakedUrl: string | null | undefined,
): string | null {
  // Key the resolved URL by the ref it was minted for, so a stale resolution
  // from a previous storage_uri is ignored once the ref changes (rather than
  // resetting state synchronously inside the effect, which cascades renders).
  const [resolved, setResolved] = useState<{ uri: string; url: string } | null>(null);

  useEffect(() => {
    if (!projectId || !storageUri) return;
    const controller = new AbortController();
    let cancelled = false;
    resolveArtifactDownloadUrl(projectId, storageUri, controller.signal)
      .then(url => { if (!cancelled) setResolved({ uri: storageUri, url }); })
      .catch(() => { /* keep the baked-URL fallback below */ });
    return () => { cancelled = true; controller.abort(); };
  }, [projectId, storageUri]);

  const fresh = resolved && storageUri && resolved.uri === storageUri ? resolved.url : null;
  return safeImageUrl(fresh) ?? safeImageUrl(bakedUrl);
}

function ResolvedImage({
  projectId,
  storageUri,
  bakedUrl,
  alt,
  className,
}: {
  projectId?: string | null;
  storageUri?: string | null;
  bakedUrl?: string | null;
  alt: string;
  className: string;
}) {
  const url = useFreshMediaUrl(projectId, storageUri, bakedUrl);
  if (!url) return null;
  return <img src={url} alt={alt} className={className} />;
}

function ResolvedVideo({
  projectId,
  storageUri,
  bakedUrl,
  ariaLabel,
  className,
}: {
  projectId?: string | null;
  storageUri?: string | null;
  bakedUrl?: string | null;
  ariaLabel: string;
  className: string;
}) {
  const url = useFreshMediaUrl(projectId, storageUri, bakedUrl);
  if (!url) return null;
  return (
    <video controls className={className} aria-label={ariaLabel}>
      <source src={url} />
      Your browser does not support the video element.
    </video>
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

      {descriptor.kind === 'image' && (() => {
        // A preview is renderable if it has EITHER a baked URL or a stable
        // storage_uri we can re-resolve — do not filter on url alone, or an
        // expired baked URL would drop a still-resolvable image.
        const listed = (descriptor.images ?? []).filter(img => img.url || img.storage_uri);
        const fallback = descriptor.image;
        const items: Array<{ url?: string | null; alt?: string | null; filename?: string | null; storage_uri?: string | null }> =
          listed.length > 0
            ? listed
            : fallback && (fallback.url || fallback.storage_uri)
              ? [{ url: fallback.url, alt: fallback.alt, filename: null, storage_uri: fallback.storage_uri }]
              : [];
        return (
          <div className="mt-2 overflow-hidden rounded border border-neutral-800 bg-neutral-950/45">
            {items.length > 1 ? (
              <div className="flex gap-1 overflow-x-auto p-1 custom-scrollbar">
                {items.map((img, i) => (
                  <ResolvedImage
                    key={img.storage_uri || img.url || i}
                    projectId={projectId}
                    storageUri={img.storage_uri}
                    bakedUrl={img.url}
                    alt={img.alt || img.filename || `${descriptor.title} ${i + 1}`}
                    className="h-36 w-auto flex-shrink-0 object-contain"
                  />
                ))}
              </div>
            ) : items.length === 1 ? (
              <ResolvedImage
                projectId={projectId}
                storageUri={items[0].storage_uri}
                bakedUrl={items[0].url}
                alt={descriptor.image?.alt || items[0].alt || descriptor.title}
                className="max-h-48 w-full object-contain"
              />
            ) : (
              <div className="px-2 py-2 text-xs text-neutral-500">
                Image preview unavailable. Open the artifact for the source.
              </div>
            )}
          </div>
        );
      })()}

      {descriptor.kind === 'video' && (() => {
        const listed = (descriptor.videos ?? []).filter(v => v.url || v.storage_uri);
        const fallback = descriptor.video;
        const items: Array<{ url?: string | null; alt?: string | null; filename?: string | null; storage_uri?: string | null }> =
          listed.length > 0
            ? listed
            : fallback && (fallback.url || fallback.storage_uri)
              ? [{ url: fallback.url, alt: fallback.alt, filename: null, storage_uri: fallback.storage_uri }]
              : [];
        return (
          <div className="mt-2 overflow-hidden rounded border border-neutral-800 bg-neutral-950/45">
            {items.length > 1 ? (
              <div className="flex gap-1 overflow-x-auto p-1 custom-scrollbar">
                {items.map((v, i) => (
                  <ResolvedVideo
                    key={v.storage_uri || v.url || i}
                    projectId={projectId}
                    storageUri={v.storage_uri}
                    bakedUrl={v.url}
                    ariaLabel={v.alt || v.filename || `${descriptor.title} ${i + 1}`}
                    className="h-36 w-auto flex-shrink-0"
                  />
                ))}
              </div>
            ) : items.length === 1 ? (
              <ResolvedVideo
                projectId={projectId}
                storageUri={items[0].storage_uri}
                bakedUrl={items[0].url}
                ariaLabel={descriptor.video?.alt || items[0].alt || descriptor.title}
                className="max-h-48 w-full"
              />
            ) : (
              <div className="px-2 py-2 text-xs text-neutral-500">
                Video preview unavailable. Open the artifact for the source.
              </div>
            )}
          </div>
        );
      })()}

      {(descriptor.kind === 'chart' || descriptor.kind === 'plot') && (
        <div className="mt-2 space-y-1.5 rounded border border-neutral-800 bg-neutral-950/45 p-2">
          {chartPoints.length > 0 ? chartPoints.slice(0, 6).map(point => {
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
