import { AlertTriangle, RefreshCw, X } from 'lucide-react';
import type { SrCatchRateHealthResponse } from '../api';

export interface SrCheckResultEntry {
  passed: boolean;
  issue?: string | null;
  origin?: string | null;
}

export interface SrMissedTask {
  task_id: string;
  title: string;
  task_type?: string | null;
  scope?: string | null;
  created_at?: string | null;
  missed_categories: string[];
  caught_categories: string[];
  pr_url?: string | null;
  sr_check_results?: Record<string, SrCheckResultEntry>;
}

const RETROSPECTIVE_DAY_OPTIONS = [7, 14, 30, 90] as const;

export { SR_CANONICAL_CATEGORIES } from './srTrendBreakdown/types';
export { SrCatchRateFreshnessPill } from './srTrendBreakdown/primitives';

const HEALTH_TOOLTIP: Record<SrCatchRateHealthResponse['status'], string> = {
  healthy: '',
  stale: 'The SR catch-rate snapshot has not been refreshed recently. Displayed metrics may lag reality.',
  frozen: 'The signal clock has stopped: catch-rate was not recomputed even though the signal timestamp advanced. Metrics are likely incorrect.',
  low_data: 'Not enough tasks have been reviewed to compute a reliable catch rate. Treat metrics as preliminary.',
  never_sufficient: 'The signal has been computed but never reached sufficient data to report a catch rate.',
  missing: 'No SR catch-rate snapshot has been computed yet for this project.',
};

export function RetrospectiveDayWindowButtons({ days, onChange }: { days: number; onChange: (days: number) => void }) {
  return (
    <>
      {RETROSPECTIVE_DAY_OPTIONS.map((day) => (
        // eslint-disable-next-line viewport/min-touch-target -- dense filter controls, consistent with adjacent panels
        <button
          key={day}
          type="button"
          onClick={() => onChange(day)}
          className={`px-1.5 py-0.5 rounded border ${days === day ? 'border-blue-500 text-blue-300' : 'border-neutral-700 text-neutral-400 hover:border-neutral-500'}`}
        >
          {day}d
        </button>
      ))}
    </>
  );
}

export function RetrospectivePagination({
  page,
  total,
  totalPages,
  pageSize,
  setPage,
}: {
  page: number;
  total: number;
  totalPages: number;
  pageSize: number;
  setPage: (page: number | ((prev: number) => number)) => void;
}) {
  if (totalPages <= 1) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 mt-2 text-ui-sm text-neutral-400">
      {/* eslint-disable-next-line viewport/min-touch-target -- compact pagination, consistent with adjacent panels */}
      <button
        type="button"
        disabled={page === 1}
        onClick={() => setPage((prev) => Math.max(1, prev - 1))}
        className="px-1.5 py-0.5 border border-neutral-700 rounded disabled:opacity-40 hover:border-neutral-500"
        aria-label="Previous page"
      >←</button>
      <span>
        Page {page} of {totalPages} · {total} total
        {total > 0 && ` · showing ${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, total)}`}
      </span>
      {/* eslint-disable-next-line viewport/min-touch-target -- compact pagination, consistent with adjacent panels */}
      <button
        type="button"
        disabled={page >= totalPages}
        onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
        className="px-1.5 py-0.5 border border-neutral-700 rounded disabled:opacity-40 hover:border-neutral-500"
        aria-label="Next page"
      >→</button>
    </div>
  );
}

export function RetrospectiveRefreshButton({ isStale, loading, onRefresh }: { isStale: boolean; loading: boolean; onRefresh: () => void }) {
  if (!isStale) return null;

  return (
    // eslint-disable-next-line viewport/min-touch-target -- dense control
    <button
      type="button"
      onClick={onRefresh}
      disabled={loading}
      className="mt-1 p-1 text-neutral-500 hover:text-neutral-300 disabled:opacity-40"
      title="Refresh data (window may have moved)"
      aria-label="Refresh pagination data"
    >
      <RefreshCw className="w-3 h-3" />
    </button>
  );
}

export function SrCatchRateHealthBanner({
  health,
  onDismiss,
}: {
  health: Pick<SrCatchRateHealthResponse, 'status' | 'last_sufficient_age_hours' | 'signal_age_hours'>;
  onDismiss: () => void;
}) {
  const isRed = health.status === 'frozen' || health.status === 'never_sufficient' || health.status === 'missing';
  const isAmber = health.status === 'stale';
  const colorClass = isRed
    ? 'border-red-800/40 bg-red-950/20 text-red-300'
    : isAmber
      ? 'border-amber-800/40 bg-amber-950/20 text-amber-300'
      : 'border-neutral-700 bg-workbench-base/50 text-neutral-400';
  const iconColor = isRed ? 'text-red-400' : isAmber ? 'text-amber-400' : 'text-neutral-500';

  let ageText = '';
  if (health.status === 'stale' || health.status === 'frozen') {
    if (health.last_sufficient_age_hours !== null) {
      ageText = ` · last sufficient ${Math.round(health.last_sufficient_age_hours)}h ago`;
    }
  } else if (health.status === 'never_sufficient') {
    if (health.signal_age_hours !== null) {
      ageText = ` · signal ${Math.round(health.signal_age_hours)}h ago`;
    }
  }

  return (
    <div
      className={`flex items-center gap-1.5 mb-2 px-2 py-1 rounded border text-ui-sm ${colorClass}`}
      role="status"
      data-testid="sr-health-banner"
      title={HEALTH_TOOLTIP[health.status]}
    >
      <AlertTriangle className={`w-3 h-3 shrink-0 ${iconColor}`} aria-hidden="true" />
      <span className="flex-1">{health.status}{ageText}</span>
      <button
        type="button"
        onClick={onDismiss}
        className="shrink-0 hover:opacity-70"
        aria-label="Dismiss health banner"
      >
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}
