import { useState, useMemo, useEffect } from 'react';
import { X, ChevronDown, ChevronRight, Compass, Search, CheckCircle, Clock, XCircle, Minus } from 'lucide-react';
import type { Task, ResearchFinding } from './TaskCard';
import { API_BASE_URL, authHeaders } from '../api';
import { parseTimestamp } from '../utils/time';

interface Direction {
  id: string;
  text: string;
  status: string;
  priority?: string;
}

/** A single finding with its provenance and outcome. */
interface EnrichedFinding {
  finding: ResearchFinding;
  taskId: string;
  taskCreatedAt: string;
  findingIndex: number;
  /** Status of the child task that implemented this finding, if any. */
  outcome?: string;
}

/** Findings grouped under a direction (or "undirected"). */
interface FindingGroup {
  directionId: string | null;
  directionText: string;
  directionStatus?: string;
  findings: EnrichedFinding[];
}

interface ResearchBrowserProps {
  tasks: Task[];
  selectedProjectId: string | null;
  onClose: () => void;
}

const OUTCOME_STYLES: Record<string, { icon: typeof CheckCircle; color: string; label: string }> = {
  COMPLETED: { icon: CheckCircle, color: 'text-emerald-500', label: 'Merged' },
  IN_PROGRESS: { icon: Clock, color: 'text-cyan-400', label: 'In progress' },
  EVALUATING: { icon: Clock, color: 'text-amber-400', label: 'Evaluating' },
  QUEUED: { icon: Clock, color: 'text-neutral-400', label: 'Queued' },
  PROVISIONING: { icon: Clock, color: 'text-neutral-400', label: 'Starting' },
  FAILED: { icon: XCircle, color: 'text-red-400', label: 'Failed' },
  CANCELLED: { icon: Minus, color: 'text-neutral-500', label: 'Cancelled' },
};

function relativeDate(dateStr: string): string {
  const diffMs = Date.now() - parseTimestamp(dateStr).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

const INCLUDED_STATUSES = new Set(['COMPLETED', 'IN_PROGRESS', 'EVALUATING']);

export function ResearchBrowser({ tasks, selectedProjectId, onClose }: ResearchBrowserProps) {
  const [search, setSearch] = useState('');
  const [expandedGroups, setExpandedGroups] = useState<Set<string> | null>(null);
  const [expandedFinding, setExpandedFinding] = useState<string | null>(null);
  const [directions, setDirections] = useState<Direction[]>([]);

  // Fetch directions for the selected project
  useEffect(() => {
    if (!selectedProjectId) return;
    let cancelled = false;
    (async () => {
      try {
        const h = await authHeaders();
        const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions`, { headers: h });
        if (res.ok && !cancelled) {
          const data = await res.json();
          setDirections(data.directions ?? data ?? []);
        }
      } catch { /* non-critical */ }
    })();
    return () => { cancelled = true; };
  }, [selectedProjectId]);

  const directionMap = useMemo(() => {
    const m = new Map<string, Direction>();
    for (const d of directions) m.set(d.id, d);
    return m;
  }, [directions]);

  // Build the map of finding key -> child task status
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

  // Group findings by direction
  const groups = useMemo((): FindingGroup[] => {
    const byDirection = new Map<string, EnrichedFinding[]>();

    for (const task of tasks) {
      if (task.task_type !== 'research' || !task.research_findings) continue;
      if (!INCLUDED_STATUSES.has(task.status)) continue;
      const findings: ResearchFinding[] = Array.isArray(task.research_findings) ? task.research_findings : [];

      for (let i = 0; i < findings.length; i++) {
        const f = findings[i];
        if (!f.title || !f.description) continue;
        // Skip non-actionable finding types
        if (f.type === 'design_doc_observation' || f.type === 'direction_proposal' || f.type === 'direction_note' || f.type === 'direction_update') continue;

        const dirId = f.direction_id || '_undirected';
        const key = `${task.id}-${i}`;
        const outcome = implementedFindings.get(key);

        if (!byDirection.has(dirId)) byDirection.set(dirId, []);
        byDirection.get(dirId)!.push({
          finding: f,
          taskId: task.id,
          taskCreatedAt: task.created_at,
          findingIndex: i,
          outcome,
        });
      }
    }

    // Sort findings within each group by date (newest first)
    for (const arr of byDirection.values()) {
      arr.sort((a, b) => parseTimestamp(b.taskCreatedAt).getTime() - parseTimestamp(a.taskCreatedAt).getTime());
    }

    // Build group objects
    const result: FindingGroup[] = [];
    for (const [dirId, findings] of byDirection) {
      const dir = dirId !== '_undirected' ? directionMap.get(dirId) : undefined;
      result.push({
        directionId: dirId === '_undirected' ? null : dirId,
        directionText: dir?.text ?? (dirId === '_undirected' ? 'Cross-cutting findings' : `Unknown direction (${dirId.slice(0, 12)})`),
        directionStatus: dir?.status,
        findings,
      });
    }

    // Sort groups: active directions first, then by finding count
    result.sort((a, b) => {
      const aActive = a.directionStatus === 'active' ? 0 : 1;
      const bActive = b.directionStatus === 'active' ? 0 : 1;
      if (aActive !== bActive) return aActive - bActive;
      return b.findings.length - a.findings.length;
    });

    return result;
  }, [tasks, implementedFindings, directionMap]);

  // Filter by search
  const filteredGroups = useMemo(() => {
    if (!search.trim()) return groups;
    const q = search.toLowerCase();
    return groups
      .map(g => ({
        ...g,
        findings: g.findings.filter(f =>
          f.finding.title.toLowerCase().includes(q) ||
          f.finding.description.toLowerCase().includes(q) ||
          (f.finding.related_files ?? []).some(p => p.toLowerCase().includes(q))
        ),
      }))
      .filter(g => g.findings.length > 0 || g.directionText.toLowerCase().includes(q));
  }, [groups, search]);

  const totalFindings = filteredGroups.reduce((sum, g) => sum + g.findings.length, 0);

  // Auto-expand first 3 groups until user interacts
  const effectiveExpanded = useMemo((): Set<string> => {
    if (expandedGroups !== null) return expandedGroups;
    return new Set(filteredGroups.slice(0, 3).map(g => g.directionId ?? '_undirected'));
  }, [expandedGroups, filteredGroups]);

  const toggleGroup = (id: string) => {
    const next = new Set(effectiveExpanded);
    if (next.has(id)) next.delete(id); else next.add(id);
    setExpandedGroups(next);
  };

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-full max-w-[600px] bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl">
        {/* Header */}
        <div className="flex justify-between items-center p-4 border-b border-neutral-800 shrink-0">
          <div>
            <h3 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
              <Compass className="w-4 h-4 text-cyan-400" />
              Findings Journal
            </h3>
            <p className="text-[10px] text-neutral-500 mt-0.5">
              {totalFindings} finding{totalFindings !== 1 ? 's' : ''} across {filteredGroups.length} direction{filteredGroups.length !== 1 ? 's' : ''}
            </p>
          </div>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Search */}
        <div className="px-4 py-2 border-b border-neutral-800/50 shrink-0">
          <div className="relative">
            <Search className="w-3.5 h-3.5 text-neutral-500 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search findings..."
              className="w-full bg-neutral-900 border border-neutral-800 rounded pl-8 pr-3 py-1.5 text-xs text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:ring-1 focus:ring-neutral-700"
            />
          </div>
        </div>

        {/* Findings grouped by direction */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {filteredGroups.length === 0 && (
            <div className="text-neutral-600 text-center py-12 text-sm">
              {groups.length === 0 ? 'No research findings yet.' : 'No findings match your search.'}
            </div>
          )}

          {filteredGroups.map(group => {
            const groupKey = group.directionId ?? '_undirected';
            const isExpanded = effectiveExpanded.has(groupKey);
            const mergedCount = group.findings.filter(f => f.outcome === 'COMPLETED').length;
            const activeCount = group.findings.filter(f => f.outcome && !['COMPLETED', 'FAILED', 'CANCELLED'].includes(f.outcome)).length;

            return (
              <div key={groupKey} className="bg-neutral-900/50 border border-neutral-800 rounded-lg overflow-hidden">
                <button
                  onClick={() => toggleGroup(groupKey)}
                  className="w-full flex items-center gap-3 px-4 py-3 hover:bg-neutral-800/30 transition-colors text-left"
                >
                  {isExpanded
                    ? <ChevronDown className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                    : <ChevronRight className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                  }
                  <div className="flex-1 min-w-0">
                    <span className="text-xs font-medium text-neutral-200 line-clamp-2 leading-relaxed">
                      {group.directionText.length > 120 ? group.directionText.slice(0, 120) + '...' : group.directionText}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 shrink-0 text-[10px]">
                    {group.directionStatus && (
                      <span className={`px-1.5 py-0.5 rounded ${
                        group.directionStatus === 'active' ? 'bg-emerald-500/15 text-emerald-400' :
                        group.directionStatus === 'paused' ? 'bg-amber-500/15 text-amber-400' :
                        'bg-neutral-700/50 text-neutral-500'
                      }`}>
                        {group.directionStatus}
                      </span>
                    )}
                    <span className="text-neutral-500 tabular-nums">
                      {group.findings.length}
                      {mergedCount > 0 && <span className="text-emerald-500 ml-1">{mergedCount} merged</span>}
                      {activeCount > 0 && <span className="text-cyan-400 ml-1">{activeCount} active</span>}
                    </span>
                  </div>
                </button>

                {isExpanded && (
                  <div className="border-t border-neutral-800 divide-y divide-neutral-800/50">
                    {group.findings.map((ef) => {
                      const outcomeInfo = ef.outcome ? OUTCOME_STYLES[ef.outcome] : null;
                      const OutcomeIcon = outcomeInfo?.icon;
                      const fKey = `${ef.taskId}-${ef.findingIndex}`;
                      const isDetailOpen = expandedFinding === fKey;
                      return (
                        <button
                          key={fKey}
                          onClick={() => setExpandedFinding(isDetailOpen ? null : fKey)}
                          className="w-full text-left px-4 py-2 hover:bg-neutral-800/20 transition-colors"
                        >
                          <div className="flex items-center gap-2">
                            {outcomeInfo && OutcomeIcon
                              ? <OutcomeIcon className={`w-3 h-3 shrink-0 ${outcomeInfo.color}`} />
                              : <span className="w-3 h-3 shrink-0 rounded-full border border-neutral-700" />
                            }
                            <span className="text-xs text-neutral-200 flex-1 truncate">{ef.finding.title}</span>
                            <span className="text-[10px] text-neutral-600 shrink-0">{relativeDate(ef.taskCreatedAt)}</span>
                          </div>
                          {isDetailOpen && (
                            <div className="mt-2 ml-5 space-y-1.5" onClick={e => e.stopPropagation()}>
                              <p className="text-[11px] text-neutral-400 leading-relaxed">{ef.finding.description}</p>
                              <div className="flex items-center gap-3 flex-wrap text-[10px] text-neutral-600">
                                {outcomeInfo && <span className={outcomeInfo.color}>{outcomeInfo.label}</span>}
                                {ef.finding.related_files && ef.finding.related_files.length > 0 && (
                                  <span className="font-mono">{ef.finding.related_files.length} file{ef.finding.related_files.length !== 1 ? 's' : ''}</span>
                                )}
                                {ef.finding.scope && ef.finding.risk && (
                                  <span className={`inline-block text-[9px] uppercase px-1 rounded ${
                                    ef.finding.risk === 'high' ? 'bg-red-900/40 text-red-400' :
                                    ef.finding.risk === 'medium' ? 'bg-amber-900/40 text-amber-400' :
                                    'bg-emerald-900/40 text-emerald-400'
                                  }`}>{ef.finding.scope}/{ef.finding.risk}</span>
                                )}
                                {ef.finding.type && (
                                  <span className="text-[10px] px-1 py-0.5 rounded bg-neutral-800 text-neutral-400">{ef.finding.type}</span>
                                )}
                                {ef.finding.priority && ef.finding.priority !== 'medium' && (
                                  <span className={ef.finding.priority === 'high' ? 'text-amber-400' : 'text-neutral-500'}>{ef.finding.priority} priority</span>
                                )}
                              </div>
                            </div>
                          )}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
