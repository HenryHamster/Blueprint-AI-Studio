import { useState, useEffect } from 'react';
import { Pencil, ChevronDown, ChevronRight, Zap } from 'lucide-react';
import { API_BASE_URL, authHeaders } from '../api';
import { timeAgo } from '../utils/time';
import { MarkdownContent } from './MarkdownContent';
import { VisionEditorModal } from './VisionEditorModal';

interface Direction {
  id: string;
  status: string;
}

interface VisionViewerProps {
  vision: Record<string, unknown>;
  directions: Direction[];
  selectedProjectId: string;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onVisionUpdated: (v: Record<string, unknown>) => void;
}

const statusColors: Record<string, string> = {
  planned: 'bg-neutral-700 text-neutral-400',
  in_progress: 'bg-amber-500/20 text-amber-400',
  functional: 'bg-emerald-500/20 text-emerald-400',
  mature: 'bg-cyan-500/20 text-cyan-400',
};

const barColors: Record<string, string> = {
  mature: 'bg-cyan-500',
  functional: 'bg-emerald-500',
  in_progress: 'bg-amber-500',
  planned: 'bg-neutral-600',
};

export default function VisionViewer({ vision, directions, selectedProjectId, addToast, onVisionUpdated }: VisionViewerProps) {
  const [showEditor, setShowEditor] = useState(false);
  const [showStrategicReview, setShowStrategicReview] = useState(false);
  const [strategicOutput, setStrategicOutput] = useState<Record<string, unknown> | null>(null);
  const [strategicTaskId, setStrategicTaskId] = useState<string | null>(null);
  const [loadingStrategic, setLoadingStrategic] = useState(false);

  const purpose = typeof vision.purpose === 'string' ? vision.purpose : '';
  const focus = Array.isArray(vision.focus) ? vision.focus as string[] : [];
  const criteria = Array.isArray(vision.success_criteria) ? vision.success_criteria as string[] : [];
  const context = typeof vision.context === 'string' ? vision.context : '';
  const systems = Array.isArray(vision.systems) ? (vision.systems as unknown[]).map((s, i) => {
    if (typeof s === 'string') return { id: `sys-${i}`, name: s, status: 'planned', description: '' };
    if (typeof s === 'object' && s !== null) return s as Record<string, unknown>;
    return null;
  }).filter((s): s is Record<string, unknown> => s !== null) : [];
  const priorities = Array.isArray(vision.priorities) ? vision.priorities as string[] : [];
  const revision = typeof vision.revision === 'number' ? vision.revision : 0;
  const lastUpdated = typeof vision.last_updated === 'string' ? vision.last_updated : '';
  const activeCount = directions.filter(d => d.status === 'active').length;
  const spawnCount = typeof vision._research_spawn_count === 'number' ? vision._research_spawn_count : 0;
  const strategicFreq = 6;
  const undirectedFreq = 4;
  const nextStrategic = strategicFreq > 0 && spawnCount > 0 ? strategicFreq - (spawnCount % strategicFreq) : strategicFreq;
  const nextUndirected = undirectedFreq > 0 && spawnCount > 0 ? undirectedFreq - (spawnCount % undirectedFreq) : undirectedFreq;

  const statusCounts = systems.reduce<Record<string, number>>((acc, s) => {
    const st = String(s.status || 'planned');
    acc[st] = (acc[st] || 0) + 1;
    return acc;
  }, {});

  // Sort systems by priority order if available
  const sortedSystems = [...systems].sort((a, b) => {
    const aIdx = priorities.indexOf(String(a.id));
    const bIdx = priorities.indexOf(String(b.id));
    if (aIdx === -1 && bIdx === -1) return 0;
    if (aIdx === -1) return 1;
    if (bIdx === -1) return -1;
    return aIdx - bIdx;
  });

  // Fetch strategic review when expanded
  useEffect(() => {
    if (!showStrategicReview || !selectedProjectId) return;
    (async () => {
      setLoadingStrategic(true);
      try {
        const h = await authHeaders();
        const res = await fetch(
          `${API_BASE_URL}/api/admin/tasks?search=${encodeURIComponent('Strategic:')}&limit=1&status=COMPLETED`,
          { headers: h }
        );
        if (res.ok) {
          const data = await res.json();
          const tasks = data.tasks ?? [];
          if (tasks.length > 0) {
            setStrategicTaskId(tasks[0].id);
            setStrategicOutput(tasks[0].plan_output ?? null);
          }
        }
      } catch { /* silent */ }
      setLoadingStrategic(false);
    })();
  }, [showStrategicReview, selectedProjectId]);

  const triggerCycle = async (phase: 'design_doc' | 'strategic_review' | 'both') => {
    try {
      const h = await authHeaders();
      const p = phase === 'both' ? 'design_doc' : phase;
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/strategic-cycle?phase=${p}`, { method: 'POST', headers: h });
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || `HTTP ${res.status}`); }
      const labels = { design_doc: 'Architecture review', strategic_review: 'Strategic review', both: 'Full cycle' };
      addToast(`${labels[phase]} triggered`, 'success');
    } catch (err) { addToast(`Failed: ${err instanceof Error ? err.message : String(err)}`, 'error'); }
  };

  if (!purpose && Object.keys(vision).length === 0) {
    return (
      <div className="p-6 flex flex-col items-center justify-center h-full">
        <div className="border border-neutral-800 border-dashed rounded-lg p-8 text-center max-w-md">
          <p className="text-sm text-neutral-500 mb-3">No vision set yet. Describe what this project should build.</p>
          <button
            onClick={() => setShowEditor(true)}
            className="text-sm text-cyan-400 hover:text-cyan-300 transition-colors font-medium"
          >
            Set initial vision
          </button>
        </div>
        {showEditor && (
          <VisionEditorModal
            projectId={selectedProjectId}
            initialVision={vision}
            onSave={(v) => { onVisionUpdated(v); setShowEditor(false); addToast('Vision updated', 'success'); }}
            onClose={() => setShowEditor(false)}
          />
        )}
      </div>
    );
  }

  return (
    <div className="p-5 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h3 className="text-base font-bold text-neutral-100 tracking-tight">Product Vision</h3>
          <div className="flex items-center gap-2 text-xs text-neutral-500">
            {revision > 0 && <span className="bg-neutral-800 px-1.5 py-0.5 rounded text-neutral-400 font-mono">v{revision}</span>}
            {lastUpdated && <span>updated {timeAgo(lastUpdated)}</span>}
            {spawnCount > 0 && (
              <span className="text-neutral-600" title={`Research cycle ${spawnCount}. Strategic in ${nextStrategic}, crosscutting in ${nextUndirected}`}>
                cycle {spawnCount} (arch {nextStrategic === strategicFreq ? 'now' : `in ${nextStrategic}`})
              </span>
            )}
          </div>
        </div>
        <button
          onClick={() => setShowEditor(true)}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs text-neutral-400 hover:text-cyan-400 hover:bg-neutral-800/50 transition-colors"
        >
          <Pencil className="w-3 h-3" />
          Edit
        </button>
      </div>

      {/* Purpose — hero section */}
      {purpose && (
        <div className="bg-gradient-to-r from-cyan-950/30 to-transparent border-l-4 border-cyan-500/60 pl-5 py-3 rounded-r-lg">
          <div className="text-base text-neutral-100 leading-relaxed font-medium">
            <MarkdownContent content={purpose} />
          </div>
        </div>
      )}

      {/* Focus — near-term priorities */}
      {focus.length > 0 && (
        <div>
          <span className="text-xs font-semibold text-neutral-400 uppercase tracking-wider">Focus</span>
          <div className="mt-2 space-y-1.5">
            {focus.map((f, i) => (
              <div key={i} className="flex items-start gap-2 text-sm">
                <span className="text-amber-500/50 mt-0.5 shrink-0">-</span>
                <span className="text-neutral-200">{String(f)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Systems — high weight */}
      {systems.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-3">
            <span className="text-sm font-bold text-neutral-300 uppercase tracking-wider">Systems</span>
            <span className="text-xs text-neutral-500 bg-neutral-800 px-1.5 py-0.5 rounded font-mono">{systems.length}</span>
          </div>

          {/* Health bar */}
          <div className="flex h-2 rounded-full overflow-hidden bg-neutral-800 mb-3">
            {['mature', 'functional', 'in_progress', 'planned'].map(status => {
              const count = statusCounts[status] || 0;
              if (count === 0) return null;
              const pct = (count / systems.length) * 100;
              return <div key={status} className={`${barColors[status]} transition-all`} style={{ width: `${pct}%` }} />;
            })}
          </div>
          <div className="flex items-center gap-3 text-[10px] text-neutral-600 mb-3">
            {statusCounts.mature ? <span className="text-cyan-500/70">{statusCounts.mature} mature</span> : null}
            {statusCounts.functional ? <span className="text-emerald-500/70">{statusCounts.functional} functional</span> : null}
            {statusCounts.in_progress ? <span className="text-amber-500/70">{statusCounts.in_progress} in progress</span> : null}
            {statusCounts.planned ? <span className="text-neutral-500">{statusCounts.planned} planned</span> : null}
          </div>

          {/* System cards */}
          <div className="space-y-1.5">
            {sortedSystems.map((sys, i) => {
              const sysStatus = String(sys.status || 'planned');
              const color = statusColors[sysStatus] || statusColors.planned;
              const desc = typeof sys.description === 'string' ? sys.description : '';
              const priorityIdx = priorities.indexOf(String(sys.id));
              return (
                <div key={String(sys.id || `sys-${i}`)} className="flex items-start gap-2.5 px-3 py-2 rounded-lg bg-neutral-900/40 border border-neutral-800/40">
                  <span className={`px-1.5 py-0.5 rounded text-[9px] font-medium shrink-0 mt-0.5 ${color}`}>
                    {sysStatus.replace('_', ' ')}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-neutral-200 font-medium">{String(sys.name || sys.id || `System ${i + 1}`)}</span>
                      {priorityIdx >= 0 && <span className="text-[9px] text-neutral-600">#{priorityIdx + 1}</span>}
                    </div>
                    {desc && <p className="text-xs text-neutral-500 mt-0.5 leading-relaxed">{desc}</p>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Success Criteria — medium weight */}
      {criteria.length > 0 && (
        <div>
          <span className="text-xs font-semibold text-neutral-400 uppercase tracking-wider">Success Criteria</span>
          <div className="mt-2 space-y-1.5">
            {criteria.map((c, i) => (
              <div key={i} className="flex items-start gap-2 text-sm">
                <span className="text-cyan-500/50 mt-0.5 shrink-0">-</span>
                <span className="text-neutral-300">{String(c)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Context — lower weight */}
      {context && (
        <div className="bg-neutral-900/40 border border-neutral-800/40 rounded-lg p-4">
          <span className="text-xs font-semibold text-neutral-500 uppercase tracking-wider">Context</span>
          <div className="mt-2">
            <MarkdownContent content={context} />
          </div>
        </div>
      )}

      {/* Stats + Actions */}
      <div className="border-t border-neutral-800/50 pt-4 space-y-3">
        <div className="flex items-center justify-between text-xs text-neutral-500">
          <span>Guiding {activeCount} active direction{activeCount !== 1 ? 's' : ''}</span>
        </div>

        {/* Strategic Review — inline collapsible */}
        <div>
          <button
            onClick={() => setShowStrategicReview(prev => !prev)}
            className="flex items-center gap-1.5 text-xs text-cyan-500 hover:text-cyan-400 transition-colors"
          >
            {showStrategicReview
              ? <ChevronDown className="w-3 h-3" />
              : <ChevronRight className="w-3 h-3" />
            }
            Last strategic review
          </button>
          {showStrategicReview && (
            <div className="mt-2 bg-neutral-900/60 border border-neutral-800 rounded-lg p-3">
              {loadingStrategic ? (
                <p className="text-xs text-neutral-500">Loading...</p>
              ) : strategicOutput ? (() => {
                const sVision = strategicOutput.vision as Record<string, unknown> | undefined;
                const sDirOps = Array.isArray(strategicOutput.direction_operations) ? strategicOutput.direction_operations as Array<Record<string, string>> : [];
                return (
                  <div className="space-y-3">
                    {sVision && (
                      <div>
                        <h4 className="text-[10px] font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">Vision Update</h4>
                        {typeof sVision.purpose === 'string' && (
                          <p className="text-xs text-neutral-300 mb-1">{sVision.purpose}</p>
                        )}
                        {Array.isArray(sVision.systems) && (
                          <div className="space-y-0.5 mt-1">
                            {(sVision.systems as Array<Record<string, string>>).map(sys => (
                              <div key={sys.id} className="flex items-center gap-2 text-[11px]">
                                <span className={`${statusColors[sys.status]?.split(' ')[1] || 'text-neutral-400'}`}>{(sys.status || 'planned').replace('_', ' ')}</span>
                                <span className="text-neutral-300">{sys.name}</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                    {sDirOps.length > 0 && (
                      <div>
                        <h4 className="text-[10px] font-semibold text-neutral-500 uppercase tracking-wider mb-1.5">Direction Changes ({sDirOps.length})</h4>
                        <div className="space-y-1.5">
                          {sDirOps.map((op, i) => {
                            const opColors: Record<string, string> = {
                              create: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
                              update: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
                              retire: 'text-red-400 bg-red-500/10 border-red-500/20',
                            };
                            const color = opColors[op.op] || 'text-neutral-400 bg-neutral-700 border-neutral-600';
                            return (
                              <div key={i} className="bg-neutral-950/60 border border-neutral-800/60 rounded p-2">
                                <div className="flex items-center gap-2">
                                  <span className={`text-[9px] font-semibold uppercase px-1.5 py-0.5 rounded border ${color}`}>{op.op}</span>
                                  {op.text && <span className="text-xs text-neutral-200 flex-1">{op.text}</span>}
                                  {op.op === 'update' && op.changes && (() => {
                                    const c = op.changes as unknown as Record<string, string>;
                                    const parts = [
                                      c.text && `text \u2192 "${c.text}"`,
                                      c.priority && `priority \u2192 ${c.priority}`,
                                      c.status && `status \u2192 ${c.status}`,
                                    ].filter(Boolean);
                                    return parts.length > 0 ? <span className="text-[11px] text-neutral-300">{parts.join(', ')}</span> : null;
                                  })()}
                                </div>
                                {op.rationale && <p className="text-[11px] text-neutral-500 mt-1">{op.rationale}</p>}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}
                    {!sVision && sDirOps.length === 0 && (
                      <p className="text-xs text-neutral-500">No structured output — the review may have failed or produced an unexpected format.</p>
                    )}
                    {strategicTaskId && (
                      <p className="text-[10px] text-neutral-600 pt-1 border-t border-neutral-800/50">Task: {strategicTaskId}</p>
                    )}
                  </div>
                );
              })() : (
                <p className="text-xs text-neutral-500">No completed strategic reviews found yet.</p>
              )}
            </div>
          )}
        </div>

        {/* Trigger buttons */}
        <div className="flex items-center gap-2">
          <button onClick={() => triggerCycle('design_doc')} className="text-[11px] px-2.5 py-1 rounded border border-purple-900/50 text-purple-400 hover:bg-purple-900/20 transition-colors flex items-center gap-1">
            <Zap className="w-3 h-3" /> Arch review
          </button>
          <button onClick={() => triggerCycle('strategic_review')} className="text-[11px] px-2.5 py-1 rounded border border-cyan-900/50 text-cyan-400 hover:bg-cyan-900/20 transition-colors flex items-center gap-1">
            <Zap className="w-3 h-3" /> Strategic only
          </button>
          <button onClick={() => triggerCycle('both')} className="text-[11px] px-2.5 py-1 rounded border border-neutral-700 text-neutral-400 hover:bg-neutral-800/50 transition-colors flex items-center gap-1">
            <Zap className="w-3 h-3" /> Full cycle
          </button>
        </div>
      </div>

      {/* Vision Editor Modal */}
      {showEditor && (
        <VisionEditorModal
          projectId={selectedProjectId}
          initialVision={vision}
          onSave={(v) => { onVisionUpdated(v); setShowEditor(false); addToast('Vision updated', 'success'); }}
          onClose={() => setShowEditor(false)}
        />
      )}
    </div>
  );
}
