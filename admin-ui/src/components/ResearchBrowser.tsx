import { useState, useMemo } from 'react';
import { X, Rocket, FileText, ChevronDown } from 'lucide-react';
import type { Task } from './TaskCard';

interface ResearchFinding {
  type?: string;
  title: string;
  description: string;
  prompt: string;
  scope: 'small' | 'medium' | 'large';
  risk: 'low' | 'medium' | 'high';
  priority: number;
  related_files: string[];
  direction_id?: string;
}

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

type SortKey = 'priority' | 'risk' | 'date';
type RiskFilter = 'all' | 'low' | 'medium' | 'high';
type ScopeFilter = 'all' | 'small' | 'medium' | 'large';

const RISK_ORDER: Record<string, number> = { high: 3, medium: 2, low: 1 };

export function ResearchBrowser({ tasks, onClose, onLaunch }: ResearchBrowserProps) {
  const [riskFilter, setRiskFilter] = useState<RiskFilter>('all');
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('all');
  const [priorityFilter, setPriorityFilter] = useState<string>('all');
  const [sortBy, setSortBy] = useState<SortKey>('priority');

  const findingsWithMeta = useMemo(() => {
    const results: { finding: ResearchFinding; taskCreatedAt: string }[] = [];
    for (const task of tasks) {
      if (task.status !== 'COMPLETED' || !task.research_findings) continue;
      try {
        const parsed = JSON.parse(task.research_findings);
        const logs: ResearchFinding[] = Array.isArray(parsed) ? parsed : parsed.logs ?? [];
        for (const f of logs) {
          if (f.title && f.prompt) {
            results.push({ finding: f, taskCreatedAt: task.created_at });
          }
        }
      } catch {
        // skip unparseable
      }
    }
    return results;
  }, [tasks]);

  const priorities = useMemo(() => {
    const set = new Set(findingsWithMeta.map(f => f.finding.priority));
    return Array.from(set).sort((a, b) => a - b);
  }, [findingsWithMeta]);

  const filtered = useMemo(() => {
    let items = findingsWithMeta;
    if (riskFilter !== 'all') items = items.filter(f => f.finding.risk === riskFilter);
    if (scopeFilter !== 'all') items = items.filter(f => f.finding.scope === scopeFilter);
    if (priorityFilter !== 'all') items = items.filter(f => String(f.finding.priority) === priorityFilter);

    items = [...items].sort((a, b) => {
      if (sortBy === 'priority') return a.finding.priority - b.finding.priority;
      if (sortBy === 'risk') return (RISK_ORDER[b.finding.risk] || 0) - (RISK_ORDER[a.finding.risk] || 0);
      return new Date(b.taskCreatedAt).getTime() - new Date(a.taskCreatedAt).getTime();
    });
    return items;
  }, [findingsWithMeta, riskFilter, scopeFilter, priorityFilter, sortBy]);

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-[680px] bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl">
        {/* Header */}
        <div className="flex justify-between items-center p-4 border-b border-neutral-800 shrink-0">
          <div>
            <h3 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
              <FileText className="w-4 h-4 text-cyan-400" />
              Research Findings
            </h3>
            <p className="text-[10px] text-neutral-500 mt-0.5">
              {filtered.length} finding{filtered.length !== 1 ? 's' : ''} from {tasks.filter(t => t.status === 'COMPLETED' && t.research_findings).length} research task{tasks.filter(t => t.status === 'COMPLETED' && t.research_findings).length !== 1 ? 's' : ''}
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
            options={[{ value: 'all', label: 'All' }, ...priorities.map(p => ({ value: String(p), label: `P${p}` }))]} />
          <div className="ml-auto">
            <FilterSelect label="Sort" value={sortBy} onChange={v => setSortBy(v as SortKey)}
              options={[{ value: 'priority', label: 'Priority' }, { value: 'risk', label: 'Risk' }, { value: 'date', label: 'Date' }]} />
          </div>
        </div>

        {/* Findings list */}
        <div className="flex-1 overflow-y-auto p-4 custom-scrollbar space-y-3">
          {filtered.length === 0 && (
            <div className="text-neutral-600 text-center py-12 text-sm">
              {findingsWithMeta.length === 0 ? 'No research findings yet.' : 'No findings match current filters.'}
            </div>
          )}
          {filtered.map((item, i) => (
            <FindingCard key={`${item.finding.direction_id || ''}-${i}`} finding={item.finding} onLaunch={onLaunch} />
          ))}
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

function FindingCard({ finding, onLaunch }: { finding: ResearchFinding; onLaunch: (f: ResearchFinding) => void }) {
  const [expanded, setExpanded] = useState(false);

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

      <button
        onClick={() => onLaunch(finding)}
        className="flex items-center gap-1.5 px-3 py-1.5 bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-400 text-xs font-medium rounded border border-cyan-500/20 hover:border-cyan-500/30 transition-all"
      >
        <Rocket className="w-3 h-3" />
        Launch
      </button>
    </div>
  );
}
