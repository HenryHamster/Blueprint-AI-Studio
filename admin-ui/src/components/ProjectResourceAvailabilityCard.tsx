import { AlertTriangle, Bot, Server } from 'lucide-react';
import { Skeleton } from './DashboardMetricCardsPrimitives';
import type { ProjectResourceSummary } from '../hooks/useProjectResourceSummary';

function Metric({ label, value, tone = 'neutral' }: { label: string; value: string | number; tone?: 'neutral' | 'good' | 'warn' }) {
  const valueClass = tone === 'good' ? 'text-emerald-300' : tone === 'warn' ? 'text-amber-300' : 'text-neutral-200';
  return (
    <div className="workbench-card flex min-w-0 items-center justify-between gap-3 rounded border px-2 py-1.5 text-xs">
      <span className="text-neutral-500">{label}</span>
      <span className={`shrink-0 font-mono font-semibold ${valueClass}`}>{value}</span>
    </div>
  );
}

function capLabel(value: number | null, suffix: string, enabledAccounts: number): string {
  if (value == null) return enabledAccounts > 0 ? 'Unbounded' : 'Not configured';
  return `${value} ${suffix}`;
}

export function ProjectResourceAvailabilityCard({
  data,
  loading,
  error,
}: {
  data: ProjectResourceSummary | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <section className="workbench-layer min-w-0 rounded-md border p-3" aria-label="Project resource availability" data-testid="project-resource-availability">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-ui-md font-bold uppercase tracking-widest text-neutral-400">Project resource availability</h3>
          <p className="mt-0.5 text-ui-sm text-neutral-500">Read-only execution capacity for this project.</p>
        </div>
      </div>

      {loading ? (
        <div className="grid gap-2 sm:grid-cols-2" aria-label="Loading project resource availability">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      ) : error || !data ? (
        <div className="flex items-center gap-2 rounded border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-xs text-amber-300">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          Project resource availability is unavailable.
        </div>
      ) : (
        <>
          <div className="grid gap-2 md:grid-cols-2">
            <div className="rounded border border-neutral-800/70 bg-neutral-950/30 p-2.5">
              <div className="mb-2 flex items-center gap-2">
                <Bot className="h-3.5 w-3.5 text-cyan-400" />
                <h4 className="text-ui-sm font-bold uppercase tracking-widest text-neutral-400">LLM</h4>
              </div>
              <div className="grid gap-1.5 sm:grid-cols-2">
                <Metric label="Configured" value={data.accounts.total} />
                <Metric label="Enabled" value={data.accounts.enabled} />
                <Metric label="Below cap" value={data.accounts.enabled_below_cap} tone={data.accounts.enabled_below_cap > 0 ? 'good' : 'warn'} />
                <Metric label="At cap" value={data.accounts.at_cap} tone={data.accounts.at_cap > 0 ? 'warn' : 'neutral'} />
                <Metric label="Activity" value={`${data.accounts.project_active_tasks} active · ${data.accounts.project_daily_task_count} today`} />
                <Metric label="Daily project cap" value={capLabel(data.accounts.project_daily_task_cap, 'tasks/day', data.accounts.enabled)} />
                <Metric label="Concurrent project cap" value={capLabel(data.accounts.project_max_concurrent_tasks, 'tasks', data.accounts.enabled)} />
              </div>
              <p className="mt-2 text-ui-xs text-neutral-600">Cap headroom only; runtime readiness is checked at dispatch.</p>
            </div>
            <div className="rounded border border-neutral-800/70 bg-neutral-950/30 p-2.5">
              <div className="mb-2 flex items-center gap-2">
                <Server className="h-3.5 w-3.5 text-violet-400" />
                <h4 className="text-ui-sm font-bold uppercase tracking-widest text-neutral-400">Runners</h4>
              </div>
              <div className="grid gap-1.5 sm:grid-cols-2">
                <Metric label="Configured" value={data.runners.total} />
                <Metric label="Online" value={data.runners.online} tone={data.runners.online > 0 ? 'good' : 'warn'} />
                <Metric label="Offline" value={data.runners.offline} tone={data.runners.offline > 0 ? 'warn' : 'neutral'} />
              </div>
            </div>
          </div>
          <p className="mt-2 text-ui-sm leading-relaxed text-neutral-500">
            Execution uses project resources; activity is attributed to the launcher; managed by the project owner.
          </p>
        </>
      )}
    </section>
  );
}
