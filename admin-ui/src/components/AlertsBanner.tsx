import { useState, useEffect, useCallback } from 'react';
import { AlertTriangle, X, ExternalLink, ChevronDown, ChevronUp, Activity, Wrench, Search, Loader2, Zap } from 'lucide-react';
import { apiFetch } from '../api';

export type { Alert } from '../types/alert';
import type { Alert } from '../types/alert';
import type { SrInvestigationSignal } from './SrTrendBreakdown';

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

interface MissedCategoryFinding {
  severity: string | null;
  description: string;
  has_more: boolean;
  full_description: string | null;
}

interface DrilldownRow {
  task_id: string;
  title: string;
  missed_categories: string[];
  sr_verdict: string | null;
  pr_url?: string | null;
  missed_category_findings?: Record<string, MissedCategoryFinding>;
}

function findingSeverityStyle(sev: string | null | undefined): string {
  if (sev === 'CRITICAL') return 'text-red-400';
  if (sev === 'HIGH') return 'text-orange-400';
  if (sev === 'MEDIUM') return 'text-amber-400';
  return 'text-neutral-400';
}

interface AlertsBannerProps {
  alerts: Alert[];
  onDismiss: (id: string) => void;
  onDismissAll?: () => void;
  onViewTask?: (taskId: string) => void;
  manualInterventionCount?: number;
  projectId?: string;
  /** Called when operator requests an SR investigation for categories in an alert. Returns spawned task id on success. */
  onSpawnSrInvestigation?: (signal: SrInvestigationSignal) => Promise<string | null>;
  /** Map from category id → open investigation task id; used to surface dedup. */
  activeInvestigationTaskIds?: Record<string, string> | null;
}

export function AlertsBanner({ alerts, onDismiss, onDismissAll, onViewTask, manualInterventionCount = 0, projectId, onSpawnSrInvestigation, activeInvestigationTaskIds }: AlertsBannerProps) {
  const [expanded, setExpanded] = useState(false);
  const [fetchedInterventionCount, setFetchedInterventionCount] = useState(0);
  const [fetchedStalledCount, setFetchedStalledCount] = useState(0);
  const [drilldownId, setDrilldownId] = useState<string | null>(null);
  const [drilldownRows, setDrilldownRows] = useState<DrilldownRow[]>([]);
  const [drilldownLoading, setDrilldownLoading] = useState(false);
  const [expandedFindings, setExpandedFindings] = useState<Set<string>>(new Set());
  const [spawningSrAlertId, setSpawningSrAlertId] = useState<string | null>(null);
  const [spawnedSrTaskIds, setSpawnedSrTaskIds] = useState<Record<string, string>>({});

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

  const handleSrInvestigate = useCallback(async (alert: Alert, srInfo: { alertType: 'trends' | 'floor'; categories: string[] }) => {
    if (!onSpawnSrInvestigation || spawningSrAlertId === alert.id) return;
    const cat = srInfo.categories.find(c => !activeInvestigationTaskIds?.[c]);
    if (!cat) return;
    setSpawningSrAlertId(alert.id);
    try {
      const signals = srInfo.alertType === 'floor' ? ['below_floor'] : ['thinning'];
      const id = await onSpawnSrInvestigation({ category: cat, signals });
      if (id) setSpawnedSrTaskIds(prev => ({ ...prev, [alert.id]: id }));
    } finally { setSpawningSrAlertId(null); }
  }, [onSpawnSrInvestigation, spawningSrAlertId, activeInvestigationTaskIds]);

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
    fetchInterventionCount();
    fetchStalledCount();
    const id = setInterval(() => {
      fetchInterventionCount();
      fetchStalledCount();
    }, 60_000);
    return () => clearInterval(id);
  }, [fetchInterventionCount, fetchStalledCount]);

  const effectiveInterventionCount = manualInterventionCount > 0 ? manualInterventionCount : fetchedInterventionCount;
  if (alerts.length === 0 && effectiveInterventionCount === 0 && fetchedStalledCount === 0) return null;

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
              className="text-xs text-neutral-400 hover:text-neutral-200 transition-colors flex items-center justify-center gap-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto px-2 md:px-0"
            >
              {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              <span className="hidden md:inline">{expanded ? 'Collapse' : `Show all ${alerts.length} alerts`}</span>
            </button>
          )}
          <button
            onClick={onDismissAll}
            className="text-xs text-neutral-400 hover:text-neutral-200 transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center px-2 md:px-0"
          >
            <span className="hidden md:inline">Dismiss all</span>
            <span className="md:hidden">✕</span>
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
                  className={`shrink-0 transition-opacity text-xs flex items-center justify-center gap-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto px-2 md:px-0 ${isOpen ? 'opacity-100' : 'opacity-60 hover:opacity-100'}`}
                  title="Investigate contributing tasks"
                >
                  <Search className="w-3.5 h-3.5" />
                  <span className="hidden md:inline">Why?</span>
                </button>
              )}
              {srInfo && onSpawnSrInvestigation && (() => {
                const existingTaskId = srInfo.categories.map(c => activeInvestigationTaskIds?.[c]).find(Boolean) ?? spawnedSrTaskIds[alert.id];
                const isSpawning = spawningSrAlertId === alert.id;
                return existingTaskId ? (
                  <span className="font-mono text-[9px] text-cyan-600 shrink-0 tabular-nums" title={`SR investigation: ${existingTaskId}`}>{existingTaskId.slice(0, 8)}</span>
                ) : (
                  <button
                    type="button"
                    onClick={() => void handleSrInvestigate(alert, srInfo)}
                    disabled={isSpawning}
                    className="shrink-0 opacity-60 hover:opacity-100 disabled:opacity-30 transition-opacity text-xs flex items-center justify-center gap-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto px-2 md:px-0"
                    title={`Spawn SR investigation for: ${srInfo.categories.join(', ')}`}
                    aria-label={`Spawn SR investigation for ${srInfo.categories.join(', ')}`}
                  >
                    {isSpawning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5" />}
                    <span className="hidden md:inline">Investigate</span>
                  </button>
                );
              })()}
              {alert.task_id && onViewTask && (() => {
                const taskId = alert.task_id;
                return (
                  <button
                    onClick={() => onViewTask(taskId)}
                    className="shrink-0 opacity-60 hover:opacity-100 transition-opacity text-xs flex items-center justify-center gap-1 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto"
                    title="View task"
                  >
                    <ExternalLink className="w-3.5 h-3.5" />
                  </button>
                );
              })()}
              <button
                onClick={() => onDismiss(alert.id)}
                className="shrink-0 opacity-60 hover:opacity-100 transition-opacity p-2.5 md:p-0 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
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
                {!drilldownLoading && drilldownRows.map(row => {
                  const findingsByCategory = row.missed_category_findings ?? {};
                  return (
                    <div key={row.task_id} className="flex items-start gap-2">
                      <span className={`shrink-0 mt-0.5 text-[10px] px-1.5 py-0.5 rounded border font-medium ${row.sr_verdict === 'pass' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' : 'bg-red-500/10 text-red-400 border-red-500/30'}`}>
                        SR {row.sr_verdict ?? '?'}
                      </span>
                      <div className="flex-1 min-w-0">
                        <span className="text-neutral-200 truncate block">{row.title}</span>
                        <div className="flex flex-col gap-0.5 mt-0.5">
                          {row.missed_categories.map(cat => {
                            const finding = findingsByCategory[cat];
                            const key = `${row.task_id}:${cat}`;
                            const isExpanded = expandedFindings.has(key);
                            const hasFinding = !!finding && !!finding.severity;
                            return (
                              <div key={cat} className="text-[10px] leading-relaxed">
                                <span className="text-neutral-500">{cat}: </span>
                                {hasFinding ? (
                                  <>
                                    <span className={`font-medium mr-1 ${findingSeverityStyle(finding.severity)}`}>{finding.severity}</span>
                                    <span className="text-neutral-400">{isExpanded && finding.full_description ? finding.full_description : finding.description}</span>
                                    {finding.has_more && (
                                      <button
                                        onClick={() => setExpandedFindings(prev => {
                                          const next = new Set(prev);
                                          if (isExpanded) { next.delete(key); } else { next.add(key); }
                                          return next;
                                        })}
                                        className="ml-1 text-neutral-500 hover:text-neutral-300 transition-colors underline"
                                      >
                                        {isExpanded ? 'less' : 'more'}
                                      </button>
                                    )}
                                  </>
                                ) : (
                                  <span className="text-neutral-600 italic">no description recorded</span>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                      {onViewTask && (
                        <button onClick={() => onViewTask(row.task_id)} className="shrink-0 opacity-60 hover:opacity-100 transition-opacity min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center" title="View task SR panel">
                          <ExternalLink className="w-3 h-3" />
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
      {hiddenCount > 0 && (
        <button
          onClick={() => setExpanded(true)}
          className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors flex items-center justify-center min-h-[44px] sm:min-h-0"
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
    </div>
  );
}
