import { useState, useEffect, useCallback } from 'react';
import { AlertTriangle, X, ExternalLink, ChevronDown, ChevronUp, Activity, Wrench, Search } from 'lucide-react';
import { apiFetch } from '../api';

export type { Alert } from '../types/alert';
import type { Alert } from '../types/alert';
import type { PipelineRegression } from './DashboardHome';

export type { PipelineRegression };

const severityStyles: Record<Alert['severity'], string> = {
  ERROR: 'bg-red-950/50 border-red-900/50 text-red-400',
  WARNING: 'bg-amber-950/50 border-amber-900/50 text-amber-400',
  INFO: 'bg-blue-950/50 border-blue-900/50 text-blue-400',
};

const iconColors: Record<Alert['severity'], string> = {
  ERROR: 'text-red-500',
  WARNING: 'text-amber-500',
  INFO: 'text-blue-500',
};

/** Max alerts shown before collapsing into a summary bar. */
const COLLAPSE_THRESHOLD = 2;

const _REGRESSION_STATUS_PILL: Record<string, string> = {
  IN_PROGRESS: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  EVALUATING: 'bg-purple-500/15 text-purple-400 border-purple-500/30',
  COMPLETED: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  APPROVED: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  FAILED: 'bg-red-500/15 text-red-400 border-red-500/30',
};

function regressionStatusLabel(status: string | null | undefined): string {
  if (!status) return 'investigating';
  const s = status.toUpperCase();
  if (s === 'IN_PROGRESS') return 'investigating';
  if (s === 'EVALUATING') return 'evaluating';
  if (s === 'COMPLETED') return 'resolved';
  if (s === 'APPROVED') return 'approved';
  if (s === 'FAILED') return 'failed';
  return status.toLowerCase();
}

const SR_TRENDS_PREFIX = 'Self-review catch-rate regression';
const SR_FLOOR_PREFIX = 'Self-review catch-rate below floor';

function parseSrAlert(title: string): { alertType: 'trends' | 'floor'; categories: string[] } | null {
  const isTrends = title.startsWith(SR_TRENDS_PREFIX);
  const isFloor = !isTrends && title.startsWith(SR_FLOOR_PREFIX);
  if (!isTrends && !isFloor) return null;
  const colonIdx = title.indexOf(': ');
  if (colonIdx === -1) return null;
  const categories = title.slice(colonIdx + 2)
    .replace(/,\s*\+\d+ more$/, '')
    .split(', ')
    .map(s => s.trim())
    .filter(Boolean);
  return { alertType: isTrends ? 'trends' : 'floor', categories };
}

interface DrilldownRow {
  task_id: string;
  title: string;
  missed_categories: string[];
  sr_verdict: string | null;
  pr_url?: string | null;
}

interface AlertsBannerProps {
  alerts: Alert[];
  onDismiss: (id: string) => void;
  onDismissAll?: () => void;
  onViewTask?: (taskId: string) => void;
  regressions?: PipelineRegression[];
  manualInterventionCount?: number;
  projectId?: string;
}

export function AlertsBanner({ alerts, onDismiss, onDismissAll, onViewTask, regressions, manualInterventionCount = 0, projectId }: AlertsBannerProps) {
  const [expanded, setExpanded] = useState(false);
  const [fetchedInterventionCount, setFetchedInterventionCount] = useState(0);
  const [fetchedStalledCount, setFetchedStalledCount] = useState(0);
  const [drilldownId, setDrilldownId] = useState<string | null>(null);
  const [drilldownRows, setDrilldownRows] = useState<DrilldownRow[]>([]);
  const [drilldownLoading, setDrilldownLoading] = useState(false);

  const fetchInterventionCount = useCallback(async () => {
    if (!projectId) return;
    try {
      const res = await apiFetch(`/api/admin/regression-cases/manual-intervention?project_id=${encodeURIComponent(projectId)}`);
      if (res.ok) {
        const data = await res.json();
        setFetchedInterventionCount((data.cases ?? []).length);
      }
    } catch { /* silent */ }
  }, [projectId]);

  const openDrilldown = useCallback(async (alert: Alert) => {
    const info = parseSrAlert(alert.title);
    if (!info || !projectId || info.categories.length === 0) return;
    if (drilldownId === alert.id) { setDrilldownId(null); return; }
    setDrilldownId(alert.id);
    setDrilldownRows([]);
    setDrilldownLoading(true);
    try {
      const params = new URLSearchParams({ alert_type: info.alertType });
      info.categories.forEach(c => params.append('categories', c));
      const res = await apiFetch(`/api/admin/projects/${projectId}/sr-alert-drilldown?${params}`);
      if (res.ok) setDrilldownRows(((await res.json()) as { items: DrilldownRow[] }).items ?? []);
    } finally {
      setDrilldownLoading(false);
    }
  }, [projectId, drilldownId]);

  const fetchStalledCount = useCallback(async () => {
    if (!projectId) return;
    try {
      const res = await apiFetch(`/api/admin/regression-cases/stalled?project_id=${encodeURIComponent(projectId)}`);
      if (res.ok) {
        const data = await res.json();
        setFetchedStalledCount((data.cases ?? []).length);
      }
    } catch { /* silent */ }
  }, [projectId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchInterventionCount();
    fetchStalledCount();
    const id = setInterval(() => {
      fetchInterventionCount();
      fetchStalledCount();
    }, 60_000);
    return () => clearInterval(id);
  }, [fetchInterventionCount, fetchStalledCount]);

  const effectiveInterventionCount = manualInterventionCount > 0 ? manualInterventionCount : fetchedInterventionCount;
  const activeRegressions = regressions?.filter(r => !r.resolved) ?? [];
  if (alerts.length === 0 && activeRegressions.length === 0 && effectiveInterventionCount === 0 && fetchedStalledCount === 0) return null;

  const shouldCollapse = alerts.length > COLLAPSE_THRESHOLD;

  const visibleAlerts = shouldCollapse && !expanded ? alerts.slice(0, 1) : alerts;
  const hiddenCount = alerts.length - visibleAlerts.length;

  return (
    <div className="mb-4 flex flex-col gap-2 shrink-0">
      {alerts.length > 1 && onDismissAll && (
        <div className="flex justify-end gap-3">
          {shouldCollapse && (
            <button
              onClick={() => setExpanded(e => !e)}
              className="text-xs text-neutral-400 hover:text-neutral-200 transition-colors flex items-center gap-1"
            >
              {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              {expanded ? 'Collapse' : `Show all ${alerts.length} alerts`}
            </button>
          )}
          <button
            onClick={onDismissAll}
            className="text-xs text-neutral-400 hover:text-neutral-200 transition-colors"
          >
            Dismiss all
          </button>
        </div>
      )}
      {visibleAlerts.map(alert => {
        const srInfo = parseSrAlert(alert.title);
        const isOpen = drilldownId === alert.id;
        return (
          <div key={alert.id} className="flex flex-col gap-1">
            <div className={`${severityStyles[alert.severity]} border rounded-lg px-4 py-3 text-sm flex items-center gap-3`}>
              <AlertTriangle className={`w-4 h-4 shrink-0 ${iconColors[alert.severity]}`} />
              <div className="flex-1 min-w-0">
                <span className="font-medium">{alert.title}</span>
                <span className="text-neutral-500 mx-2">&middot;</span>
                <span className="opacity-80">{alert.message}</span>
              </div>
              {srInfo && srInfo.categories.length > 0 && projectId && (
                <button
                  onClick={() => void openDrilldown(alert)}
                  className={`shrink-0 transition-opacity text-xs flex items-center gap-1 ${isOpen ? 'opacity-100' : 'opacity-60 hover:opacity-100'}`}
                  title="Investigate contributing tasks"
                >
                  <Search className="w-3.5 h-3.5" />
                  <span>Why?</span>
                </button>
              )}
              {alert.task_id && onViewTask && (() => {
                const taskId = alert.task_id;
                return (
                  <button
                    onClick={() => onViewTask(taskId)}
                    className="shrink-0 opacity-60 hover:opacity-100 transition-opacity text-xs flex items-center gap-1"
                    title="View task"
                  >
                    <ExternalLink className="w-3.5 h-3.5" />
                  </button>
                );
              })()}
              <button
                onClick={() => onDismiss(alert.id)}
                className="shrink-0 opacity-60 hover:opacity-100 transition-opacity"
                title="Dismiss"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            {isOpen && (
              <div className="bg-neutral-900/60 border border-neutral-700/40 rounded-lg px-4 py-3 text-xs text-neutral-300 flex flex-col gap-2">
                <span className="text-neutral-500 text-[11px] font-medium uppercase tracking-wide">Contributing tasks</span>
                {drilldownLoading && <span className="text-neutral-500">Loading…</span>}
                {!drilldownLoading && drilldownRows.length === 0 && <span className="text-neutral-500">No contributing tasks found in the current alert window.</span>}
                {!drilldownLoading && drilldownRows.map(row => (
                  <div key={row.task_id} className="flex items-start gap-2">
                    <span className={`shrink-0 mt-0.5 text-[10px] px-1.5 py-0.5 rounded border font-medium ${row.sr_verdict === 'pass' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' : 'bg-red-500/10 text-red-400 border-red-500/30'}`}>
                      SR {row.sr_verdict ?? '?'}
                    </span>
                    <div className="flex-1 min-w-0">
                      <span className="text-neutral-200 truncate block">{row.title}</span>
                      <span className="text-neutral-500 text-[10px]">missed: {row.missed_categories.join(', ')}</span>
                    </div>
                    {onViewTask && (
                      <button onClick={() => onViewTask(row.task_id)} className="shrink-0 opacity-60 hover:opacity-100 transition-opacity" title="View task SR panel">
                        <ExternalLink className="w-3 h-3" />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
      {hiddenCount > 0 && (
        <button
          onClick={() => setExpanded(true)}
          className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors text-center py-1"
        >
          +{hiddenCount} more alert{hiddenCount > 1 ? 's' : ''}
        </button>
      )}
      {effectiveInterventionCount > 0 && (
        <div className="bg-red-950/30 border border-red-900/40 rounded-lg px-4 py-2 text-sm flex items-center gap-3">
          <Wrench className="w-4 h-4 shrink-0 text-red-400" />
          <div className="flex-1 min-w-0 flex items-center gap-2">
            <span className="font-medium text-red-300">Manual Intervention Required</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded border bg-red-500/15 text-red-400 border-red-500/30 font-medium">
              {effectiveInterventionCount} regression{effectiveInterventionCount > 1 ? 's' : ''}
            </span>
          </div>
        </div>
      )}
      {fetchedStalledCount > 0 && (
        <div className="bg-yellow-950/30 border border-yellow-900/40 rounded-lg px-4 py-2 text-sm flex items-center gap-3">
          <Activity className="w-4 h-4 shrink-0 text-yellow-500" />
          <div className="flex-1 min-w-0 flex items-center gap-2">
            <span className="font-medium text-yellow-300">Stalled Regression</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded border bg-yellow-500/15 text-yellow-400 border-yellow-500/30 font-medium">
              {fetchedStalledCount} case{fetchedStalledCount > 1 ? 's' : ''}
            </span>
          </div>
        </div>
      )}
      {activeRegressions.map(r => {
        const pillStyle = r.spawned_task_status
          ? (_REGRESSION_STATUS_PILL[r.spawned_task_status.toUpperCase()] ?? _REGRESSION_STATUS_PILL['IN_PROGRESS'])
          : 'bg-neutral-800/50 text-neutral-400 border-neutral-700/50';
        const taskId = r.spawned_task_id;
        return (
          <div
            key={`regression-${r.signal_name}`}
            className="bg-orange-950/30 border border-orange-900/40 rounded-lg px-4 py-2 text-sm flex items-center gap-3"
          >
            <Activity className="w-4 h-4 shrink-0 text-orange-500" />
            <div className="flex-1 min-w-0 flex items-center gap-2 flex-wrap">
              <span className="font-medium text-orange-300 capitalize">{r.signal_name.replace(/_/g, ' ')}</span>
              {r.regression_class && (
                <span className="text-[10px] text-neutral-500 font-mono">{r.regression_class}</span>
              )}
              <span
                className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${taskId ? pillStyle : 'bg-neutral-800/50 text-neutral-500 border-neutral-700/50'}`}
              >
                {taskId ? regressionStatusLabel(r.spawned_task_status) : 'no action'}
              </span>
            </div>
            {taskId && onViewTask && (
              <button
                onClick={() => onViewTask(taskId)}
                className="shrink-0 opacity-60 hover:opacity-100 transition-opacity text-xs flex items-center gap-1"
                title={r.spawned_task_title ?? 'View investigation task'}
              >
                <ExternalLink className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
