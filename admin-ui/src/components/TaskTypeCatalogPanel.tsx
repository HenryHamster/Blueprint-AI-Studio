import { useState, useEffect, useCallback } from 'react';
import { AlertTriangle, Boxes, ChevronDown, ChevronUp, Rocket } from 'lucide-react';
import {
  fetchTaskTypeCatalog,
  fetchTaskTypeCostAdherence,
  type TaskTypeCatalogEntry,
  type TaskTypeCostAdherenceEntry,
} from '../api';

interface TaskTypeCatalogPanelProps {
  /**
   * When provided, the panel renders these entries directly and skips the
   * network fetch. Used by tests and by callers that already hold the catalog.
   */
  entries?: TaskTypeCatalogEntry[];
  /**
   * Optional per-type actual-cost adherence rows, keyed by task-type id. When
   * provided alongside ``entries`` the panel renders them directly and skips the
   * adherence fetch (used by tests).
   */
  adherence?: TaskTypeCostAdherenceEntry[];
}

const OUTPUT_KIND_LABELS: Record<TaskTypeCatalogEntry['output_kind'], string> = {
  pr: 'Pull request',
  artifact: 'Artifact',
  knowledge: 'Knowledge',
  verdict: 'Verdict',
  none: '—',
};

const RUN_SHAPE_LABELS: Record<TaskTypeCatalogEntry['run_shape'], string> = {
  single_agent: 'Single agent',
  workflow: 'Multi-agent workflow',
  experiment_loop: 'Experiment loop',
};

const EVALUATION_LABELS: Record<TaskTypeCatalogEntry['evaluation_strategy'], string> = {
  ci_and_review: 'CI + review',
  review_only: 'Review',
  forward_quality: 'Forward quality',
  metric: 'Metric',
  none: 'Not gated',
};

function Requirement({ label, active }: { label: string; active: boolean }) {
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded ${
        active ? 'bg-sky-900/50 text-sky-300' : 'bg-neutral-800/60 text-neutral-600 line-through'
      }`}
    >
      {label}
    </span>
  );
}

function fmtUsd(value: number | null | undefined): string {
  return value != null ? `$${value.toFixed(2)}` : '—';
}

function CatalogCard({
  entry,
  adherence,
}: {
  entry: TaskTypeCatalogEntry;
  adherence?: TaskTypeCostAdherenceEntry;
}) {
  const hasSamples = adherence != null && adherence.sample_count > 0;
  const over = adherence?.over_ceiling ?? false;
  const actualLabel =
    adherence != null && adherence.sample_count > 0
      ? `${fmtUsd(adherence.avg_cost_usd)} / ${fmtUsd(adherence.p95_cost_usd)}`
      : '—';
  return (
    <div
      data-testid="task-type-card"
      data-task-type={entry.id}
      className="min-w-0 bg-neutral-900/50 border border-neutral-800/60 rounded-lg p-3 flex flex-col gap-2"
    >
      <div className="flex items-center gap-2 flex-wrap min-w-0">
        <span className="font-medium text-sm text-neutral-200 truncate">{entry.label}</span>
        <code className="text-[10px] font-mono text-neutral-500 truncate">{entry.id}</code>
        <div className="flex-1" />
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${
            entry.agent_mode === 'write'
              ? 'bg-amber-900/50 text-amber-300'
              : 'bg-neutral-800 text-neutral-400'
          }`}
        >
          {entry.agent_mode === 'write' ? 'write' : 'read-only'}
        </span>
        {entry.user_launchable && (
          <span
            data-testid="launchable-badge"
            className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/50 text-emerald-300 shrink-0"
          >
            <Rocket className="w-2.5 h-2.5" />launchable
          </span>
        )}
      </div>

      <p className="text-xs text-neutral-400 leading-relaxed line-clamp-3" data-viewport-allow-truncation>
        {entry.description || 'No description.'}
      </p>

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
        <dt className="text-neutral-500">Runs as</dt>
        <dd className="text-neutral-300 truncate text-right">{RUN_SHAPE_LABELS[entry.run_shape]}</dd>
        <dt className="text-neutral-500">Output</dt>
        <dd className="text-neutral-300 truncate text-right">{OUTPUT_KIND_LABELS[entry.output_kind]}</dd>
        <dt className="text-neutral-500">Evaluated by</dt>
        <dd className="text-neutral-300 truncate text-right">{EVALUATION_LABELS[entry.evaluation_strategy]}</dd>
        <dt className="text-neutral-500">Cost ceiling</dt>
        <dd className="text-neutral-300 truncate text-right tabular-nums">
          {fmtUsd(entry.cost_ceiling_usd)}
        </dd>
        <dt className="text-neutral-500">Actual avg / p95</dt>
        <dd
          data-testid="actual-cost"
          className={`truncate text-right tabular-nums ${over ? 'text-red-400 font-medium' : 'text-neutral-300'}`}
        >
          {hasSamples ? (
            <span className="inline-flex items-center gap-1 justify-end">
              {over && <AlertTriangle data-testid="over-ceiling-badge" className="w-3 h-3 shrink-0" />}
              {actualLabel}
            </span>
          ) : (
            '—'
          )}
        </dd>
        <dt className="text-neutral-500">Samples ({adherence ? 'recent' : '—'})</dt>
        <dd className="text-neutral-300 truncate text-right tabular-nums">
          {adherence ? adherence.sample_count : '—'}
        </dd>
      </dl>

      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-[10px] text-neutral-500">needs</span>
        <Requirement label="code" active={entry.requires_code} />
        <Requirement label="web" active={entry.requires_web} />
        <Requirement label="compute" active={entry.requires_compute} />
      </div>
    </div>
  );
}

export function TaskTypeCatalogPanel({ entries, adherence }: TaskTypeCatalogPanelProps) {
  const injected = entries !== undefined;
  const [collapsed, setCollapsed] = useState(() =>
    localStorage.getItem('blueprint-admin:taskTypeCatalogCollapsed') !== 'false'
  );
  const [catalog, setCatalog] = useState<TaskTypeCatalogEntry[]>(entries ?? []);
  const [adherenceMap, setAdherenceMap] = useState<Record<string, TaskTypeCostAdherenceEntry>>(
    () => Object.fromEntries((adherence ?? []).map(a => [a.id, a]))
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (injected) return;
    setLoading(true);
    setError(null);
    try {
      // Fetch the static catalog and the DB-backed adherence rows in parallel.
      // Adherence failure must not blank the catalog — degrade to "—" instead.
      const [list, adherenceRows] = await Promise.all([
        fetchTaskTypeCatalog(),
        fetchTaskTypeCostAdherence().catch(() => [] as TaskTypeCostAdherenceEntry[]),
      ]);
      setCatalog(list);
      setAdherenceMap(Object.fromEntries(adherenceRows.map(a => [a.id, a])));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load task-type catalog');
    } finally {
      setLoading(false);
    }
  }, [injected]);

  useEffect(() => {
    if (injected) {
      setCatalog(entries ?? []);
      setAdherenceMap(Object.fromEntries((adherence ?? []).map(a => [a.id, a])));
      return;
    }
    if (!collapsed && catalog.length === 0) load();
  }, [collapsed, injected, entries, adherence, catalog.length, load]);

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem('blueprint-admin:taskTypeCatalogCollapsed', String(next));
  };

  const launchableCount = catalog.filter(e => e.user_launchable).length;

  return (
    <div className="mb-4 bg-neutral-950 border border-neutral-800/60 rounded-xl overflow-hidden shrink-0">
      <button
        onClick={toggle}
        className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-neutral-900/50 transition-colors"
      >
        <Boxes className="w-4 h-4 text-sky-400 shrink-0" />
        <span className="font-medium text-sm text-neutral-200">Task-Type Catalog</span>
        {catalog.length > 0 && (
          <span className="ml-1 text-xs text-neutral-500">
            {catalog.length} types · {launchableCount} launchable
          </span>
        )}
        <div className="flex-1" />
        {collapsed ? <ChevronDown className="w-4 h-4 text-neutral-500" /> : <ChevronUp className="w-4 h-4 text-neutral-500" />}
      </button>

      {!collapsed && (
        <div className="border-t border-neutral-800/60 p-3">
          {loading && catalog.length === 0 && (
            <div className="px-2 py-6 text-center text-sm text-neutral-500">Loading…</div>
          )}
          {error && (
            <div className="px-2 py-2 text-xs text-red-400 bg-red-950/30 rounded">{error}</div>
          )}
          {!loading && catalog.length === 0 && !error && (
            <div className="px-2 py-6 text-center text-sm text-neutral-500">No task types registered.</div>
          )}
          {catalog.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {catalog.map(entry => (
                <CatalogCard key={entry.id} entry={entry} adherence={adherenceMap[entry.id]} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
