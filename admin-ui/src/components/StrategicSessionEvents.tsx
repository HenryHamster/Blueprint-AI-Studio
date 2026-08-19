import { ArrowUpRight } from 'lucide-react';
import type {
  StrategicContextPackageDrilldown,
  StrategicWorkSessionEvent,
} from '../strategicStateApi';

type StrategicContextTarget = StrategicContextPackageDrilldown['target'];

type Density = 'compact' | 'comfortable';

interface StrategicSessionEventsProps {
  events?: StrategicWorkSessionEvent[] | null;
  limit?: number;
  density?: Density;
  emptyLabel?: string;
  onOpenTarget: (target?: StrategicContextTarget | null) => void;
}

function toneDotClass(tone?: string | null): string {
  if (tone === 'critical') return 'bg-rose-400';
  if (tone === 'warning') return 'bg-amber-300';
  if (tone === 'active') return 'bg-cyan-400';
  if (tone === 'watch') return 'bg-workbench-hover';
  return 'bg-workbench-card';
}

function safeDate(value?: string | null): Date | null {
  if (!value) return null;
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatRelativeTime(value?: string | null): string | null {
  const date = safeDate(value);
  if (!date) return null;
  const diffMs = Date.now() - date.getTime();
  const absMs = Math.abs(diffMs);
  const suffix = diffMs < -60_000 ? 'from now' : 'ago';
  const minutes = Math.floor(absMs / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ${suffix}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${suffix}`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ${suffix}`;
  return date.toLocaleDateString();
}

function eventMeta(event: StrategicWorkSessionEvent): string {
  return [
    event.source,
    event.resource.replace(/_/g, ' '),
    formatRelativeTime(event.occurred_at),
  ].filter(Boolean).join(' / ');
}

function rowClass(density: Density, canOpen: boolean): string {
  const base = density === 'compact'
    ? 'grid min-h-11 w-full grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-2 rounded-md border border-neutral-800 bg-workbench-shell/50 px-2.5 py-2 text-left'
    : 'grid min-h-12 w-full grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-2 rounded-md border border-neutral-800 bg-workbench-shell/50 px-3 py-2.5 text-left';
  return canOpen
    ? `${base} transition-colors hover:border-neutral-700 hover:bg-workbench-hover`
    : base;
}

function EventRow({
  event,
  density,
  onOpenTarget,
}: {
  event: StrategicWorkSessionEvent;
  density: Density;
  onOpenTarget: (target?: StrategicContextTarget | null) => void;
}) {
  const canOpen = Boolean(event.target);
  const content = (
    <>
      <span className={`mt-1.5 h-1.5 w-1.5 rounded-full ${toneDotClass(event.tone)}`} aria-hidden="true" />
      <span className="min-w-0">
        <span className="block truncate text-xs font-semibold text-neutral-100">{event.title}</span>
        <span className="mt-0.5 block truncate font-mono text-ui-sm uppercase text-neutral-500">
          {eventMeta(event)}
        </span>
        {event.summary && (
          <span className="mt-0.5 block line-clamp-2 text-xs leading-relaxed text-neutral-500">
            {event.summary}
          </span>
        )}
      </span>
      {canOpen ? (
        <ArrowUpRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
      ) : (
        <span aria-hidden="true" />
      )}
    </>
  );

  if (!canOpen) {
    return (
      <div className={rowClass(density, false)} data-testid="strategic-session-event">
        {content}
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => onOpenTarget(event.target)}
      className={rowClass(density, true)}
      title={`Open ${event.title}`}
      data-testid="strategic-session-event"
    >
      {content}
    </button>
  );
}

export function StrategicSessionEvents({
  events,
  limit = 6,
  density = 'comfortable',
  emptyLabel,
  onOpenTarget,
}: StrategicSessionEventsProps) {
  const visibleEvents = (events ?? []).slice(0, Math.max(0, limit));
  if (visibleEvents.length === 0) {
    if (!emptyLabel) return null;
    return (
      <div className="rounded-md border border-dashed border-neutral-800 px-3 py-4 text-sm text-neutral-500">
        {emptyLabel}
      </div>
    );
  }
  return (
    <div className="space-y-1.5" data-testid="strategic-session-events">
      {visibleEvents.map(event => (
        <EventRow
          key={event.id}
          event={event}
          density={density}
          onOpenTarget={onOpenTarget}
        />
      ))}
    </div>
  );
}
