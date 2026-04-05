import { useState, useMemo } from 'react';
import { X, Rocket, FileText, ChevronDown, ChevronRight } from 'lucide-react';
import type { Task, ResearchFinding } from './TaskCard';

type ResearchGroup = {
  taskId: string;
  taskTitle: string;
  taskCreatedAt: string;
  taskStatus: string;
  findings: { finding: ResearchFinding; findingIndex: number }[];
};

interface ResearchBrowserProps {
  tasks: Task[];
  onClose: () => void;
  onLaunch: (finding: ResearchFinding) => void;
}

const RISK_COLORS: Record<string, string> = {
  low: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  medium: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  high: 'bg-red-500/20 text-red-400 border-red-500/30',
};

const SCOPE_COLORS: Record<string, string> = {
  small: 'bg-neutral-700/50 text-neutral-300',
  medium: 'bg-blue-500/20 text-blue-400',
  large: 'bg-purple-500/20 text-purple-400',
};

const STATUS_BADGE: Record<string, string> = {
  COMPLETED: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  IN_PROGRESS: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  EVALUATING: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  QUEUED: 'bg-neutral-700/50 text-neutral-400 border-neutral-700',
  PROVISIONING: 'bg-neutral-700/50 text-neutral-400 border-neutral-700',
  FAILED: 'bg-red-500/20 text-red-400 border-red-500/30',
  CANCELLED: 'bg-neutral-700/50 text-neutral-400 border-neutral-700',
};

type SortKey = 'priority' | 'risk' | 'date';
type RiskFilter = 'all' | 'low' | 'medium' | 'high';
type ScopeFilter = 'all' | 'small' | 'medium' | 'large';

const RISK_ORDER: Record<string, number> = { high: 3, medium: 2, low: 1 };
const PRIORITY_ORDER: Record<string, number> = { high: 3, medium: 2, low: 1 };
const INCLUDED_STATUSES = new Set(['COMPLETED', 'IN_PROGRESS', 'EVALUATING']);

function relativeDate(dateStr: string): string {
  const diffMs = Date.now() - new Date(dateStr).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin} minute${diffMin !== 1 ? 's' : ''} ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} hour${diffHr !== 1 ? 's' : ''} ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay} day${diffDay !== 1 ? 's' : ''} ago`;
}

export function ResearchBrowser({ tasks, onClose, onLaunch }: ResearchBrowserProps) {
  const [riskFilter, setRiskFilter] = useState<RiskFilter>('all');
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('all');
  const [priorityFilter, setPriorityFilter] = useState<string>('all');
  const [sortBy, setSortBy] = useState<SortKey>('priority');
  const [expandedGroups, setExpandedGroups] = useState<Set<string> | null>(null);

  const groups = useMemo((): ResearchGroup[] => {
    const result: ResearchGroup[] = [];
    for (const task of tasks) {
      if (task.task_type !== 'research' || !task.research_findings) continue;
      if (!INCLUDED_STATUSES.has(task.status)) continue;
      const logs: ResearchFinding[] = Array.isArray(task.research_findings) ? task.research_findings : [];
      const findings = logs
        .map((f, i) => ({ finding: f, findingIndex: i }))
        .filter(({ finding: f }) => f.title && f.prompt);
      if (findings.length > 0) {
        result.push({
          taskId: task.id,
          taskTitle: task.title,
          taskCreatedAt: task.created_at,
          taskStatus: task.status,
          findings,
        });
      }
    }
    result.sort((a, b) => new Date(b.taskCreatedAt).getTime() - new Date(a.taskCreatedAt).getTime());
    return result;
  }, [tasks]);

  const implementedFindings = useMemo(() => {
    const map = new Map<string, string>();
    for (const task of tasks) {
      const ctx = task.context as Record<string, unknown> | undefined;
      if (ctx?.research_task_id && ctx?.finding_index !== undefined) {
        const key = `${ctx.research_task_id}-${ctx.finding_index}`;
        map.set(key, task.status);
      }
    }
    return map;
  }, [tasks]);

  const priorities = useMemo(() => {
    const set = new Set(groups.flatMap(g => g.findings.map(f => f.finding.priority)));
    return Array.from(set).sort((a, b) => (PRIORITY_ORDER[b] ?? 0) - (PRIORITY_ORDER[a] ?? 0));
  }, [groups]);

  const effectiveExpanded = useMemo((): Set<string> => {
    if (expandedGroups !== null) return expandedGroups;
    return groups.length > 0 ? new Set([groups[0].taskId]) : new Set();
  }, [expandedGroups, groups]);

  const toggleGroup = (taskId: string) => {
    const next = new Set(effectiveExpanded);
    if (next.has(taskId)) next.delete(taskId);
    else next.add(taskId);
    setExpandedGroups(next);
  };

  const filteredGroups = useMemo(() => {
    return groups.map(group => {
      let findings = group.findings;
      if (riskFilter !== 'all') findings = findings.filter(f => f.finding.risk === riskFilter);
      if (scopeFilter !== 'all') findings = findings.filter(f => f.finding.scope === scopeFilter);
      if (priorityFilter !== 'all') findings = findings.filter(f => String(f.finding.priority) === priorityFilter);

      if (sortBy !== 'date') {
        findings = [...findings].sort((a, b) =>
          sortBy === 'priority'
            ? (PRIORITY_ORDER[b.finding.priority] ?? 0) - (PRIORITY_ORDER[a.finding.priority] ?? 0)
            : (RISK_ORDER[b.finding.risk] || 0) - (RISK_ORDER[a.finding.risk] || 0)
        );
      }

      return { ...group, findings };
    }).filter(g => g.findings.length > 0);
  }, [groups, riskFilter, scopeFilter, priorityFilter, sortBy]);

  const totalFindings = filteredGroups.reduce((sum, g) => sum + g.findings.length, 0);
  const taskCount = filteredGroups.length;

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-full max-w-[680px] bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl overflow-y-auto">
        {/* Header */}
        <div className="flex justify-between items-center p-4 border-b border-neutral-800 shrink-0">
          <div>
            <h3 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
              <FileText className="w-4 h-4 text-cyan-400" />
              Research Findings
            </h3>
            <p className="text-[10px] text-neutral-500 mt-0.5">
              {taskCount} research task{taskCount !== 1 ? 's' : ''}, {totalFindings} finding{totalFindings !== 1 ? 's' : ''} total
            </p>
          </div>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Filters */}
        <div className="px-4 py-3 border-b border-neutral-800/50 flex items-center gap-3 shrink-0 flex-wrap">
          <FilterSelect label="Risk" value={riskFilter} onChange={v => setRiskFilter(v as RiskFilter)}
            options={[{ value: 'all', label: 'All' }, { value: 'low', label: 'Low' }, { value: 'medium', label: 'Medium' }, { value: 'high', label: 'High' }]} />
          <FilterSelect label="Scope" value={scopeFilter} onChange={v => setScopeFilter(v as ScopeFilter)}
            options={[{ value: 'all', label: 'All' }, { value: 'small', label: 'Small' }, { value: 'medium', label: 'Medium' }, { value: 'large', label: 'Large' }]} />
          <FilterSelect label="Priority" value={priorityFilter} onChange={v => setPriorityFilter(v)}
            options={[{ value: 'all', label: 'All' }, ...priorities.map(p => ({ value: String(p), label: String(p) }))]} />
          <div className="ml-auto">
            <FilterSelect label="Sort" value={sortBy} onChange={v => setSortBy(v as SortKey)}
              options={[{ value: 'priority', label: 'Priority' }, { value: 'risk', label: 'Risk' }, { value: 'date', label: 'Date' }]} />
          </div>
        </div>

        {/* Groups list */}
        <div className="flex-1 overflow-y-auto p-4 custom-scrollbar space-y-2">
          {filteredGroups.length === 0 && (
            <div className="text-neutral-600 text-center py-12 text-sm">
              {groups.length === 0 ? 'No research findings yet.' : 'No findings match current filters.'}
            </div>
          )}
          {filteredGroups.map((group, groupIndex) => {
            const isExpanded = effectiveExpanded.has(group.taskId);
            const showSeparator = filteredGroups.length >= 2 && groupIndex === 1;

            return (
              <div key={group.taskId}>
                {showSeparator && (
                  <div className="flex items-center gap-3 py-2 my-1">
                    <div className="flex-1 h-px bg-neutral-800" />
                    <span className="text-[10px] text-neutral-600 font-semibold uppercase tracking-wider">Older research</span>
                    <div className="flex-1 h-px bg-neutral-800" />
                  </div>
                )}
                <div className={`bg-neutral-900 border rounded-lg overflow-hidden transition-colors ${groupIndex === 0 ? 'border-neutral-700' : 'border-neutral-800'}`}>
                  <button
                    onClick={() => toggleGroup(group.taskId)}
                    className="w-full flex items-center gap-3 p-3 hover:bg-neutral-800/50 transition-colors text-left"
                  >
                    {isExpanded
                      ? <ChevronDown className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                      : <ChevronRight className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                    }
                    <div className="flex-1 min-w-0">
                      <span className="text-xs font-medium text-neutral-200 truncate block">
                        Research: {group.taskTitle}
                      </span>
                      <span className="text-[10px] text-neutral-500">{relativeDate(group.taskCreatedAt)}</span>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${STATUS_BADGE[group.taskStatus] ?? STATUS_BADGE.QUEUED} ${group.taskStatus === 'IN_PROGRESS' ? 'animate-pulse' : ''}`}>
                        {group.taskStatus.replace('_', ' ')}
                      </span>
                      <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400">
                        {group.findings.length} finding{group.findings.length !== 1 ? 's' : ''}
                      </span>
                    </div>
                  </button>
                  {isExpanded && (
                    <div className="px-3 pb-3 pt-2 space-y-2 border-t border-neutral-800">
                      {group.findings.map(({ finding, findingIndex }) => {
                        const key = `${group.taskId}-${findingIndex}`;
                        const implementedStatus = implementedFindings.get(key);
                        return (
                          <FindingCard
                            key={`${finding.direction_id || ''}-${findingIndex}`}
                            finding={finding}
                            onLaunch={onLaunch}
                            implementedStatus={implementedStatus}
                          />
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function FilterSelect({ label, value, onChange, options }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider">{label}</span>
      <div className="relative">
        <select
          value={value}
          onChange={e => onChange(e.target.value)}
          className="appearance-none bg-neutral-900 border border-neutral-800 text-neutral-300 rounded px-2 py-1 pr-6 text-[11px] focus:outline-none focus:ring-1 focus:ring-neutral-700 cursor-pointer"
        >
          {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <ChevronDown className="w-3 h-3 text-neutral-500 absolute right-1.5 top-1/2 -translate-y-1/2 pointer-events-none" />
      </div>
    </div>
  );
}

function FindingCard({ finding, onLaunch, implementedStatus }: { finding: ResearchFinding; onLaunch: (f: ResearchFinding) => void; implementedStatus?: string }) {
  const [expanded, setExpanded] = useState(false);

  const isCompleted = implementedStatus === 'COMPLETED';
  const isInProgress = implementedStatus === 'QUEUED' || implementedStatus === 'IN_PROGRESS' || implementedStatus === 'PROVISIONING' || implementedStatus === 'EVALUATING';
  const isFailed = implementedStatus === 'FAILED';
  const isCancelled = implementedStatus === 'CANCELLED';

  return (
    <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-4 hover:border-neutral-700 transition-colors">
      <div className="flex items-start justify-between gap-3 mb-2">
        <h4 className="text-sm font-medium text-neutral-100 leading-snug flex-1">{finding.title}</h4>
        <div className="flex items-center gap-1.5 shrink-0">
          <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${RISK_COLORS[finding.risk] || RISK_COLORS.low}`}>
            {finding.risk?.toUpperCase()}
          </span>
          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${SCOPE_COLORS[finding.scope] || SCOPE_COLORS.small}`}>
            {finding.scope}
          </span>
        </div>
      </div>

      <p className="text-xs text-neutral-400 leading-relaxed mb-3">{finding.description}</p>

      <div className="flex items-center gap-3 mb-3 text-[10px] text-neutral-500">
        <span className="font-mono">Priority: {finding.priority}</span>
        {finding.type && <span className="font-mono">Type: {finding.type}</span>}
        {finding.direction_id && (
          <span className="font-mono px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-400 border border-indigo-500/30">
            {finding.direction_id}
          </span>
        )}
      </div>

      {finding.related_files?.length > 0 && (
        <div className="mb-3">
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[10px] text-neutral-500 hover:text-neutral-300 transition-colors flex items-center gap-1"
          >
            <ChevronDown className={`w-3 h-3 transition-transform ${expanded ? 'rotate-0' : '-rotate-90'}`} />
            {finding.related_files.length} related file{finding.related_files.length !== 1 ? 's' : ''}
          </button>
          {expanded && (
            <div className="mt-1.5 pl-4 space-y-0.5">
              {finding.related_files.map((f, i) => (
                <div key={i} className="text-[11px] text-neutral-500 font-mono truncate">{f}</div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="flex items-center gap-2">
        {isCompleted && (
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-emerald-500/20 text-emerald-400 border-emerald-500/30">
            Implemented
          </span>
        )}
        {isInProgress && (
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-yellow-500/20 text-yellow-400 border-yellow-500/30">
            {implementedStatus === 'IN_PROGRESS' ? 'In Progress' : implementedStatus === 'QUEUED' ? 'Queued' : implementedStatus === 'PROVISIONING' ? 'Provisioning' : 'Evaluating'}
          </span>
        )}
        {isFailed && (
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-red-500/20 text-red-400 border-red-500/30">
            Failed
          </span>
        )}
        {isCancelled && (
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-neutral-700/50 text-neutral-400 border-neutral-700">
            Cancelled
          </span>
        )}
        {!isCompleted && (
          <button
            onClick={() => onLaunch(finding)}
            disabled={isInProgress}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded border transition-all ${isInProgress ? 'bg-neutral-800 text-neutral-500 border-neutral-700 cursor-not-allowed' : 'bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-400 border-cyan-500/20 hover:border-cyan-500/30'}`}
          >
            <Rocket className="w-3 h-3" />
            Launch
          </button>
        )}
      </div>
    </div>
  );
}
