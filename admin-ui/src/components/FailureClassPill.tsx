import { useState, useEffect, useRef } from 'react';
import { ResponsiveContainer, AreaChart, Area } from 'recharts';
import { FAILURE_CLASS_COLORS } from './DashboardMetricCardsPrimitives';
import { TAXONOMY_COLORS, TAXONOMY_LABELS } from './dashboardConstants';
import type { FailureGroup } from '../types/digest';
import { Badge } from './ui/Badge';

const FAILURE_CLASS_STROKE: Record<string, string> = {
  CODE_ERROR: '#f87171',
  PROVISION_FAILURE: '#fb923c',
  AGENT_TIMEOUT: '#fbbf24',
  RATE_LIMIT: '#facc15',
  EXTERNAL_API: '#c084fc',
  SCOPE_OVERFLOW: '#2dd4bf',
};

const TIMEOUT_SUBTYPE_LABELS: Record<string, string> = {
  heartbeat_lost: 'heartbeat lost',
  oom: 'OOM',
  execution_timeout: 'exec timeout',
  cascade_cancellation: 'cascade',
  iteration_limit: 'iter limit',
  process_crash: 'crash',
  unknown: 'unknown',
};

export function FailureClassPill({ group, sparkData, cost }: { group: FailureGroup; sparkData?: { v: number }[]; cost?: number }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const colorClass = FAILURE_CLASS_COLORS[group.failure_class] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
  const stroke = FAILURE_CLASS_STROKE[group.failure_class] ?? '#a3a3a3';
  const topTasks = group.tasks.slice(0, 3);
  const hasSpark = sparkData && sparkData.length >= 3;

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        className={`inline-flex items-center justify-center gap-1.5 text-ui-sm font-semibold px-3 py-2 rounded border min-h-11 ${colorClass} cursor-pointer`}
        onClick={() => setOpen(v => !v)}
        title={`${group.label}: ${group.count} failure${group.count !== 1 ? 's' : ''}`}
      >
        {group.label}
        <span className="font-bold">{group.count}</span>
        {cost != null && cost > 0 && (
          <span className="font-mono text-ui-xs opacity-80">${cost.toFixed(2)}</span>
        )}
        {hasSpark && (
          <span className="w-16 h-3 inline-block">
            <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
              <AreaChart data={sparkData}>
                <defs>
                  <linearGradient id={`fsp-${group.failure_class}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={stroke} stopOpacity={0.4} />
                    <stop offset="100%" stopColor={stroke} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <Area type="monotone" dataKey="v" stroke={stroke} fill={`url(#fsp-${group.failure_class})`} strokeWidth={1} dot={false} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </span>
        )}
      </button>
      {open && topTasks.length > 0 && (
        <div className="absolute z-10 top-full mt-1 left-0 w-64 bg-workbench-base border border-neutral-700 rounded-lg p-2 shadow-lg">
          <div className="space-y-1">
            {topTasks.map(t => (
              <div key={t.task_id} className="text-ui-sm text-neutral-300 flex items-center gap-1 min-w-0">
                {t.taxonomy && TAXONOMY_LABELS[t.taxonomy] && (
                  <span className={`shrink-0 text-ui-xs leading-none px-1 py-0.5 rounded border ${TAXONOMY_COLORS[t.taxonomy] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30'}`} title={TAXONOMY_LABELS[t.taxonomy]}>
                    {TAXONOMY_LABELS[t.taxonomy]}
                  </span>
                )}
                <span className="truncate">{t.title}</span>
                {group.failure_class === 'AGENT_TIMEOUT' && t.timeout_subtype && (
                  <Badge
                    tone="warning"
                    title={`Timeout subtype: ${t.timeout_subtype}`}
                    className="shrink-0 font-mono opacity-80"
                  >
                    {TIMEOUT_SUBTYPE_LABELS[t.timeout_subtype] ?? t.timeout_subtype}
                  </Badge>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
