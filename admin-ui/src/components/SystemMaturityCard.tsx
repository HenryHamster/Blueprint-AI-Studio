import { useState } from 'react';
import { timeAgo } from '../utils/time';

// ---------- types ----------

export interface SystemMaturityRow {
  system_id: string;
  score: number;
  recommended_mode: string | null;
  top_issues: Array<Record<string, unknown>>;
  last_audited_at: string;
}

// ---------- constants ----------

const MATURITY_MODE_STYLES: Record<string, string> = {
  audit: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  fundamentals: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  ship: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  pause: 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30',
};

function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse bg-workbench-panel/60 rounded ${className}`} />;
}

// ---------- component ----------

export function SystemMaturityCard({ rows, loading }: { rows: SystemMaturityRow[] | null; loading: boolean }) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">System Maturity</h2>
      {loading || rows === null ? (
        <Skeleton className="h-16 w-full" />
      ) : rows.length === 0 ? (
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">
          No audits have run yet
        </div>
      ) : (
        <div className="space-y-1.5">
          {rows.map(row => {
            const scoreColor = row.score >= 70 ? 'bg-emerald-500' : row.score >= 40 ? 'bg-amber-500' : 'bg-red-500';
            const modeStyle = row.recommended_mode
              ? (MATURITY_MODE_STYLES[row.recommended_mode] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30')
              : null;
            const isExpanded = expandedId === row.system_id;
            const hasIssues = (row.top_issues ?? []).length > 0;
            return (
              <div key={row.system_id} className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
                <div className="flex items-center gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1.5">
                      <span className="text-xs font-medium text-neutral-200 truncate">{row.system_id}</span>
                      {modeStyle && (
                        <span className={`shrink-0 text-ui-xs font-bold uppercase px-1.5 py-0.5 rounded border ${modeStyle}`}>
                          {row.recommended_mode}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-1.5 rounded-full bg-workbench-panel overflow-hidden">
                        <div className={`h-full rounded-full ${scoreColor}`} style={{ width: `${row.score}%` }} />
                      </div>
                      <span className="shrink-0 text-ui-sm font-mono text-neutral-400">{row.score}</span>
                      <span className="shrink-0 text-ui-sm text-neutral-500">{timeAgo(row.last_audited_at)}</span>
                    </div>
                  </div>
                  {hasIssues && (
                    <button
                      type="button"
                      className="shrink-0 text-ui-sm text-neutral-500 hover:text-neutral-300 transition-colors ml-1"
                      onClick={() => setExpandedId(isExpanded ? null : row.system_id)}
                    >
                      {isExpanded ? 'hide' : `${row.top_issues.length} issues`}
                    </button>
                  )}
                </div>
                {isExpanded && hasIssues && (
                  <div className="mt-2 space-y-0.5 pt-2 border-t border-neutral-800/50">
                    {row.top_issues.map((issue, i) => {
                      const desc = (issue.description as string | undefined) ?? JSON.stringify(issue);
                      return (
                        <div key={i} className="text-ui-sm text-neutral-400 flex items-start gap-1.5">
                          <span className="shrink-0 text-neutral-600">•</span>
                          <span>{desc}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
