import { useState, useEffect, useCallback } from 'react';
import { AlertTriangle, Info, CheckCircle } from 'lucide-react';
import { apiFetch } from '../api';
import type { HealthOverview, HealthOverviewIssue } from '../types/diagnostics';

interface HealthOverviewCardProps {
  selectedProjectId: string | null;
  onViewTask: (taskId: string) => void;
  addToast?: (msg: string, type?: 'error' | 'success' | 'info') => void;
}

function statusDotColor(status: HealthOverview['overall_status']): string {
  if (status === 'healthy') return 'bg-emerald-500';
  if (status === 'degraded') return 'bg-amber-400 animate-pulse';
  return 'bg-red-500 animate-pulse';
}

function statusTextColor(status: HealthOverview['overall_status']): string {
  if (status === 'healthy') return 'text-emerald-400';
  if (status === 'degraded') return 'text-amber-400';
  return 'text-red-400';
}

function statusLabel(status: HealthOverview['overall_status']): string {
  if (status === 'healthy') return 'Healthy';
  if (status === 'degraded') return 'Degraded';
  return 'Unhealthy';
}

function severityIcon(severity: HealthOverviewIssue['severity']) {
  if (severity === 'info') return <Info className="w-3 h-3 text-blue-400 shrink-0 mt-px" />;
  if (severity === 'warning') return <AlertTriangle className="w-3 h-3 text-amber-400 shrink-0 mt-px" />;
  return <AlertTriangle className="w-3 h-3 text-red-400 shrink-0 mt-px" />;
}

function trendArrow(trend: HealthOverview['success_rate']['trend']): { symbol: string; color: string } {
  if (trend === 'improving') return { symbol: '↑', color: 'text-emerald-400' };
  if (trend === 'declining') return { symbol: '↓', color: 'text-red-400' };
  return { symbol: '→', color: 'text-neutral-500' };
}

// Splits hint text and replaces task ID tokens (8-char hex) with clickable spans
function HintWithTaskLinks({ hint, taskIds, onViewTask }: { hint: string; taskIds: string[]; onViewTask: (id: string) => void }) {
  if (taskIds.length === 0) return <span className="text-neutral-500">{hint}</span>;

  const parts: React.ReactNode[] = [];
  let remaining = hint;
  for (const id of taskIds) {
    const idx = remaining.indexOf(id);
    if (idx === -1) continue;
    if (idx > 0) parts.push(remaining.slice(0, idx));
    parts.push(
      <span
        key={id}
        className="text-blue-400 cursor-pointer hover:text-blue-300 underline underline-offset-2"
        onClick={() => onViewTask(id)}
      >
        {id}
      </span>
    );
    remaining = remaining.slice(idx + id.length);
  }
  if (remaining) parts.push(remaining);
  return <span className="text-neutral-500">{parts}</span>;
}

export function HealthOverviewCard({ selectedProjectId, onViewTask }: HealthOverviewCardProps) {
  const [data, setData] = useState<HealthOverview | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`/api/admin/health-overview?${params}`);
      if (res.ok) {
        const json: HealthOverview = await res.json();
        setData(json);
      }
    } catch {
      // silently fail — show last known state
    }
  }, [selectedProjectId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchData();
    const id = setInterval(() => {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      fetchData();
    }, 30000);
    return () => clearInterval(id);
  }, [fetchData]);

  if (!data) return null;

  const arrow = trendArrow(data.success_rate.trend);
  const portfolio = data.direction_portfolio;
  const issues = data.actionable_issues.slice(0, 3);

  return (
    <div className="flex items-start gap-4 px-3 py-2 bg-neutral-900 border border-neutral-800 rounded-lg text-xs mb-3 shrink-0">
      {/* Left: overall status */}
      <div className="flex items-center gap-1.5 shrink-0 self-center min-w-[90px]">
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDotColor(data.overall_status)}`} />
        <span className={`font-medium ${statusTextColor(data.overall_status)}`}>
          {statusLabel(data.overall_status)}
        </span>
      </div>

      <div className="w-px self-stretch bg-neutral-800 shrink-0" />

      {/* Center: actionable issues */}
      <div className="flex-1 min-w-0">
        {issues.length === 0 ? (
          <div className="flex items-center gap-1.5 text-emerald-400">
            <CheckCircle className="w-3 h-3 shrink-0" />
            <span>All systems healthy</span>
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {issues.map((issue, i) => (
              <div key={i} className="flex items-start gap-1.5 min-w-0">
                {severityIcon(issue.severity)}
                <span className="text-neutral-300 shrink-0">{issue.title}</span>
                <span className="text-neutral-600 shrink-0 mx-0.5">—</span>
                <span className="min-w-0 truncate">
                  <HintWithTaskLinks hint={issue.hint} taskIds={issue.task_ids} onViewTask={onViewTask} />
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="w-px self-stretch bg-neutral-800 shrink-0" />

      {/* Right: success rate + direction portfolio */}
      <div className="flex flex-col gap-1 shrink-0 items-end">
        <div className="flex items-center gap-1">
          <span className="text-neutral-400">Success</span>
          <span className="text-neutral-200 font-medium">{Math.round((data.success_rate.current ?? 0) * 100)}%</span>
          <span className={`font-medium ${arrow.color}`}>{arrow.symbol}</span>
        </div>
        {portfolio.total_active > 0 && (
          <div className="flex items-center gap-1 text-neutral-500">
            {portfolio.product > 0 && (
              <span className="px-1 py-px bg-neutral-800 rounded text-neutral-400">
                {portfolio.product} product
              </span>
            )}
            {portfolio.structural > 0 && (
              <span className="px-1 py-px bg-neutral-800 rounded text-neutral-400">
                {portfolio.structural} structural
              </span>
            )}
            {portfolio.mixed > 0 && (
              <span className="px-1 py-px bg-neutral-800 rounded text-neutral-400">
                {portfolio.mixed} mixed
              </span>
            )}
            {portfolio.uncategorized > 0 && (
              <span className="px-1 py-px bg-neutral-800 rounded text-neutral-400">
                {portfolio.uncategorized} other
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
