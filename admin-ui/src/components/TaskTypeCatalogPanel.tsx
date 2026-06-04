import { useState, useEffect, useCallback } from 'react';
import { Boxes, ChevronDown, ChevronUp, Rocket } from 'lucide-react';
import { fetchTaskTypeCatalog, type TaskTypeCatalogEntry } from '../api';

interface TaskTypeCatalogPanelProps {
  /**
   * When provided, the panel renders these entries directly and skips the
   * network fetch. Used by tests and by callers that already hold the catalog.
   */
  entries?: TaskTypeCatalogEntry[];
}

const OUTPUT_KIND_LABELS: Record<TaskTypeCatalogEntry['output_kind'], string> = {
  pr: 'Pull request',
  artifact: 'Artifact',
  knowledge: 'Knowledge',
  verdict: 'Verdict',
  none: '—',
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

function CatalogCard({ entry }: { entry: TaskTypeCatalogEntry }) {
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
        <dt className="text-neutral-500">Output</dt>
        <dd className="text-neutral-300 truncate text-right">{OUTPUT_KIND_LABELS[entry.output_kind]}</dd>
        <dt className="text-neutral-500">Evaluated by</dt>
        <dd className="text-neutral-300 truncate text-right">{EVALUATION_LABELS[entry.evaluation_strategy]}</dd>
        <dt className="text-neutral-500">Cost ceiling</dt>
        <dd className="text-neutral-300 truncate text-right tabular-nums">
          {entry.cost_ceiling_usd != null ? `$${entry.cost_ceiling_usd.toFixed(2)}` : '—'}
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

export function TaskTypeCatalogPanel({ entries }: TaskTypeCatalogPanelProps) {
  const injected = entries !== undefined;
  const [collapsed, setCollapsed] = useState(() =>
    localStorage.getItem('blueprint-admin:taskTypeCatalogCollapsed') !== 'false'
  );
  const [catalog, setCatalog] = useState<TaskTypeCatalogEntry[]>(entries ?? []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (injected) return;
    setLoading(true);
    setError(null);
    try {
      const list = await fetchTaskTypeCatalog();
      setCatalog(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load task-type catalog');
    } finally {
      setLoading(false);
    }
  }, [injected]);

  useEffect(() => {
    if (injected) {
      setCatalog(entries ?? []);
      return;
    }
    if (!collapsed && catalog.length === 0) load();
  }, [collapsed, injected, entries, catalog.length, load]);

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
                <CatalogCard key={entry.id} entry={entry} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
