import { useState, useEffect } from 'react';
import { Plus, X, ToggleLeft, ToggleRight, ChevronDown, ChevronRight } from 'lucide-react';
import { apiFetch } from '../api';
import DesignDocViewer from './DesignDocViewer';
import VisionViewer from './VisionViewer';
import { DirectionCard } from './DirectionCard';
import { AutonomousConfigSection } from './AutonomousConfigSection';
import { useDirectionForm } from '../hooks/useDirectionForm';
import { useAutonomousConfig } from '../hooks/useAutonomousConfig';
import { formatRelativeTime } from '../utils/directionHelpers';

interface Project {
  id: string;
  name?: string;
  [key: string]: unknown;
}

export interface ResearchHistoryEntry {
  timestamp: string;
  task_id: string;
  tasks_created: number;
  finding_titles: string[];
  key_finding_descriptions: string[];
  tasks_completed?: number;
  tasks_failed?: number;
  cycle_insights?: string[];
  product_ratio?: number | null;
  type_distribution?: Record<string, number> | null;
  prs_merged?: number | null;
  prs_rejected?: number | null;
  taxonomy_summary?: Record<string, number> | null;
}

export interface Direction {
  id: string;
  text: string;
  priority: 'high' | 'medium' | 'low';
  added_at: string;
  status: 'active' | 'completed' | 'paused' | 'proposed' | 'expired';
  category?: 'product' | 'structural' | 'mixed' | null;
  paths?: string[];
  last_researched_at?: string | null;
  proposed_by?: string;
  proposal_rationale?: string;
  proposed_at?: string;
  research_history?: ResearchHistoryEntry[];
  possibly_saturated?: boolean;
  possibly_complete?: boolean;
  completion_suggestion?: string;
  pending_update?: {
    suggested_text?: string;
    suggested_priority?: string;
    suggested_paths?: string[];
    rationale: string;
    proposed_at: string;
    proposed_by: string;
  };
  notes?: Array<{
    text: string;
    added_at: string;
    added_by_task: string;
  }>;
  paused_reason?: string;
  paused_at?: string;
  auto_paused?: boolean;
  /**
   * Productivity score: (merged − 0.5 × rejected) / cycles.
   * Quality-weighted — can go negative when rejections outweigh merges.
   * Backend key is `merge_rate` for historical reasons; treat as productivity score.
   */
  merge_rate?: number | null;
  history_aggregate?: {
    total_prs_merged?: number | null;
    total_prs_rejected?: number | null;
    total_tasks_failed?: number | null;
    [key: string]: unknown;
  } | null;
}


const STATUS_GROUPS: Array<{ key: 'proposed' | 'active' | 'paused' | 'completed' | 'expired'; label: string }> = [
  { key: 'proposed', label: 'Proposed by Agent' },
  { key: 'active', label: 'Active' },
  { key: 'paused', label: 'Paused' },
  { key: 'completed', label: 'Completed' },
  { key: 'expired', label: 'Expired' },
];

const priorityOrder: Record<string, number> = { high: 0, medium: 1, low: 2 };

export function DirectionsPanel({ onClose, addToast, onViewFindings, onNavigateToTask }: { onClose: () => void; addToast: (message: string, type: 'success' | 'error' | 'info') => void; onViewFindings?: (directionId: string) => void; onNavigateToTask?: (taskId: string) => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [directions, setDirections] = useState<Direction[]>([]);
  const [autonomousEnabled, setAutonomousEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [taskCounts, setTaskCounts] = useState<Record<string, number>>({});
  const [activeTasks, setActiveTasks] = useState<Record<string, Array<{ id: string; status: string; title: string; latest_status_message?: string }>>>({});
  const [vision, setVision] = useState<Record<string, unknown>>({});
  const [designDoc, setDesignDoc] = useState<Record<string, unknown>>({});
  const [activeTab, setActiveTab] = useState<'directions' | 'vision' | 'design-doc'>('directions');

  // Error feedback
  const [error, setError] = useState<string | null>(null);

  // Collapsible sections — expired collapsed by default, others expanded
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({ expired: true });

  // Research history open state per direction
  const [historyOpenIds, setHistoryOpenIds] = useState<Record<string, boolean>>({});

  // Research button state
  const [researchingIds, setResearchingIds] = useState<Record<string, boolean>>({});
  const [researchSuccessIds, setResearchSuccessIds] = useState<Record<string, boolean>>({});

  // Force-research confirmation popover
  const [forceConfirmId, setForceConfirmId] = useState<string | null>(null);

  // Category filter
  const [categoryFilter, setCategoryFilter] = useState<'all' | 'product' | 'structural' | 'mixed'>('all');

  // Form state for add/edit
  const form = useDirectionForm();

  // Autonomous config state + handlers
  const config = useAutonomousConfig({ setError, addToast });

  useEffect(() => {
    fetchProjects();
    config.fetchCooldownSetting();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (selectedProjectId) {
      fetchDirections();
      config.fetchSettings();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  async function fetchProjects() {
    try {
      const res = await apiFetch('/api/admin/projects');
      if (!res.ok) throw new Error('Failed to fetch projects');
      const data = await res.json();
      const list = Array.isArray(data) ? data : data.projects ?? [];
      setProjects(list);
      if (list.length > 0 && !selectedProjectId) setSelectedProjectId(list[0].id);
    } catch (err) {
      console.error('Failed to fetch projects:', err);
      addToast('Failed to load projects', 'error');
    }
  }

  async function fetchDirections() {
    setLoading(true);
    try {
      const [res, countsRes] = await Promise.all([
        apiFetch(`/api/admin/projects/${selectedProjectId}/directions`),
        apiFetch(`/api/admin/projects/${selectedProjectId}/directions/task-counts`),
      ]);
      if (!res.ok) throw new Error('Failed to fetch directions');
      const data = await res.json();
      setDirections(data.directions ?? []);
      setAutonomousEnabled(data.autonomous_enabled ?? false);
      setVision(data.vision ?? {});
      setDesignDoc(data.design_doc ?? {});
      if (countsRes.ok) {
        const countsData = await countsRes.json();
        setTaskCounts(countsData.counts ?? {});
        setActiveTasks(countsData.active_tasks ?? {});
      }
    } catch (err) {
      console.error('Failed to fetch directions:', err);
      addToast('Failed to load directions', 'error');
    }
    setLoading(false);
  }

  async function addDirection() {
    if (!form.newText.trim() || !selectedProjectId) return;
    setError(null);
    try {
      const body: Record<string, unknown> = { text: form.newText.trim(), priority: form.newPriority, paths: form.newPathTags };
      if (form.newCategory) body.category = form.newCategory;
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('Failed to add direction');
      form.resetAddForm();
      fetchDirections();
    } catch {
      setError('Failed to add direction. Please try again.');
    }
  }

  async function updateDirection(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ text: form.editText, priority: form.editPriority, status: form.editStatus, paths: form.editPathTags, category: form.editCategory ?? null }),
      });
      if (!res.ok) throw new Error('Failed to update direction');
      form.setEditingId(null);
      fetchDirections();
    } catch {
      setError('Failed to update direction. Please try again.');
    }
  }

  async function quickToggleStatus(id: string, currentStatus: 'active' | 'paused') {
    setError(null);
    const newStatus = currentStatus === 'active' ? 'paused' : 'active';
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) throw new Error('Failed to update status');
      fetchDirections();
    } catch {
      setError('Failed to update status. Please try again.');
    }
  }

  async function reactivateDirection(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      });
      if (!res.ok) throw new Error('Failed to reactivate direction');
      fetchDirections();
    } catch {
      setError('Failed to reactivate direction. Please try again.');
    }
  }

  async function markDirectionComplete(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'completed' }),
      });
      if (!res.ok) throw new Error('Failed to mark direction as complete');
      fetchDirections();
    } catch {
      setError('Failed to mark direction as complete. Please try again.');
    }
  }

  async function approveProposal(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      });
      if (!res.ok) throw new Error('Failed to approve proposal');
      addToast('Direction approved and activated', 'success');
      fetchDirections();
    } catch {
      setError('Failed to approve proposal. Please try again.');
    }
  }

  async function rejectProposal(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('Failed to reject proposal');
      addToast('Direction proposal rejected', 'info');
      fetchDirections();
    } catch {
      setError('Failed to reject proposal. Please try again.');
    }
  }

  async function reviveProposal(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'proposed' }),
      });
      if (!res.ok) throw new Error('Failed to revive proposal');
      addToast('Expired proposal revived', 'success');
      fetchDirections();
    } catch {
      setError('Failed to revive proposal. Please try again.');
    }
  }

  async function applyDirectionUpdate(d: Direction) {
    if (!d.pending_update) return;
    setError(null);
    try {
      const updated: Record<string, unknown> = { pending_update: null };
      if (d.pending_update.suggested_text) updated.text = d.pending_update.suggested_text;
      if (d.pending_update.suggested_priority) updated.priority = d.pending_update.suggested_priority;
      if (d.pending_update.suggested_paths) updated.paths = d.pending_update.suggested_paths;
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${d.id}`, {
        method: 'PATCH',
        body: JSON.stringify(updated),
      });
      if (!res.ok) throw new Error('Failed to apply direction update');
      fetchDirections();
    } catch {
      setError('Failed to apply direction update. Please try again.');
    }
  }

  async function dismissDirectionUpdate(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ pending_update: null }),
      });
      if (!res.ok) throw new Error('Failed to dismiss direction update');
      fetchDirections();
    } catch {
      setError('Failed to dismiss direction update. Please try again.');
    }
  }

  async function deleteDirection(id: string) {
    if (!window.confirm('Are you sure you want to delete this direction?')) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('Failed to delete direction');
      fetchDirections();
    } catch {
      setError('Failed to delete direction. Please try again.');
    }
  }

  async function toggleAutonomous() {
    setError(null);
    const newValue = !autonomousEnabled;
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/autonomous`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: newValue }),
      });
      if (!res.ok) throw new Error('Failed to toggle autonomous mode');
      setAutonomousEnabled(newValue);
    } catch {
      setError('Failed to toggle autonomous mode. Please try again.');
    }
  }

  async function reorderDirections(newOrder: Direction[]) {
    const previousDirections = directions;
    setDirections(prev => {
      const nonActive = prev.filter(d => d.status !== 'active');
      return [...newOrder, ...nonActive];
    });
    setError(null);
    try {
      const nonActive = previousDirections.filter(d => d.status !== 'active');
      const allOrdered = [...newOrder, ...nonActive];
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/reorder`, {
        method: 'PATCH',
        body: JSON.stringify({ direction_ids: allOrdered.map(d => d.id) }),
      });
      if (!res.ok) throw new Error('Failed to reorder directions');
    } catch {
      setDirections(previousDirections);
      setError('Failed to reorder directions. Please try again.');
    }
  }

  async function triggerResearch(id: string, bypass = false) {
    setResearchingIds(prev => ({ ...prev, [id]: true }));
    setForceConfirmId(null);
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}/research${bypass ? '?bypass_cooldown=true' : ''}`, { method: 'POST' });
      if (!res.ok) throw new Error('Failed to trigger research');
      setResearchSuccessIds(prev => ({ ...prev, [id]: true }));
      setTimeout(() => setResearchSuccessIds(prev => ({ ...prev, [id]: false })), 3000);
    } catch {
      setError('Failed to trigger research. Please try again.');
    } finally {
      setResearchingIds(prev => ({ ...prev, [id]: false }));
    }
  }

  function toggleSection(key: string) {
    setCollapsed(prev => ({ ...prev, [key]: !prev[key] }));
  }

  const categoryFilterFn = (d: Direction) =>
    categoryFilter === 'all' || d.category === categoryFilter;

  const grouped = STATUS_GROUPS.map(({ key, label }) => ({
    key,
    label,
    items: key === 'active'
      ? directions.filter(d => d.status === key).filter(categoryFilterFn)
      : directions
          .filter(d => d.status === key)
          .filter(categoryFilterFn)
          .sort((a, b) => priorityOrder[a.priority] - priorityOrder[b.priority]),
  }));

  const proposedCount = directions.filter(d => d.status === 'proposed').length;
  const activeItems = (grouped.find(g => g.key === 'active') ?? { items: [] }).items;

  return (
    <div className="fixed inset-0 z-[60] flex">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/80" onClick={onClose} />

      {/* Panel */}
      <div className={`relative ml-auto w-full ${activeTab === 'directions' ? 'sm:max-w-lg' : 'sm:max-w-3xl'} h-full bg-neutral-950 border-l border-neutral-800 flex flex-col overflow-hidden transition-[max-width] duration-200`}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-neutral-800 shrink-0">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold text-neutral-100 uppercase tracking-wider">Strategic</h2>
            {proposedCount > 0 && activeTab === 'directions' && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full font-semibold bg-purple-950/60 text-purple-400 border border-purple-900/50">
                {proposedCount} pending
              </span>
            )}
          </div>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Project selector (shared) */}
        <div className="px-5 py-2 border-b border-neutral-800 shrink-0">
          <select
            value={selectedProjectId}
            onChange={e => setSelectedProjectId(e.target.value)}
            className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
          >
            {projects.map(p => (
              <option key={p.id} value={p.id}>{p.name || p.id}</option>
            ))}
          </select>
        </div>

        {/* Tabs */}
        <div className="flex items-center border-b border-neutral-800 shrink-0">
          <button
            onClick={() => setActiveTab('directions')}
            className={`px-4 py-2.5 text-sm font-medium transition-colors ${
              activeTab === 'directions'
                ? 'text-neutral-100 border-b-2 border-blue-500'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            Directions
          </button>
          <button
            onClick={() => setActiveTab('vision')}
            className={`px-4 py-2.5 text-sm font-medium transition-colors ${
              activeTab === 'vision'
                ? 'text-neutral-100 border-b-2 border-cyan-500'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            Vision
          </button>
          <button
            onClick={() => setActiveTab('design-doc')}
            className={`px-4 py-2.5 text-sm font-medium transition-colors ${
              activeTab === 'design-doc'
                ? 'text-neutral-100 border-b-2 border-purple-500'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            Design Doc
            {(() => {
              const pc = Object.keys(designDoc).filter(k => !k.startsWith('_')).length;
              return pc > 0 ? <span className="ml-1.5 text-xs text-neutral-500">({pc})</span> : null;
            })()}
          </button>
        </div>

        {/* === Directions tab content === */}
        {activeTab === 'directions' && (<>
        {/* Autonomous toggle + config */}
        <div className="px-5 py-3 border-b border-neutral-800 space-y-3 shrink-0">
          {selectedProjectId && (
            <>
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-medium text-neutral-400 uppercase tracking-wider">Autonomous Mode</span>
                <button onClick={toggleAutonomous} className="flex items-center gap-1.5 text-sm transition-colors">
                  {autonomousEnabled ? (
                    <><ToggleRight className="w-6 h-6 text-emerald-400" /><span className="text-emerald-400 text-xs font-medium">On</span></>
                  ) : (
                    <><ToggleLeft className="w-6 h-6 text-neutral-600" /><span className="text-neutral-500 text-xs font-medium">Off</span></>
                  )}
                </button>
              </div>
              <p className="text-xs text-neutral-500 leading-relaxed">
                Active directions guide the research agent. Only Active directions are included in new tasks.
              </p>

              <AutonomousConfigSection
                configCooldownHours={config.configCooldownHours}
                setConfigCooldownHours={config.setConfigCooldownHours}
                configIntervalMinutes={config.configIntervalMinutes}
                setConfigIntervalMinutes={config.setConfigIntervalMinutes}
                configMaxParallel={config.configMaxParallel}
                setConfigMaxParallel={config.setConfigMaxParallel}
                configMaxTasksPerHour={config.configMaxTasksPerHour}
                setConfigMaxTasksPerHour={config.setConfigMaxTasksPerHour}
                autonomousConfigOpen={config.autonomousConfigOpen}
                setAutonomousConfigOpen={config.setAutonomousConfigOpen}
                savingConfigField={config.savingConfigField}
                saveConfigField={config.saveConfigField}
              />
            </>
          )}
        </div>

        {/* Add direction form */}
        {selectedProjectId && (
          <div className="px-5 py-3 border-b border-neutral-800 shrink-0">
            <div className="flex gap-2">
              <input
                value={form.newText}
                onChange={e => form.setNewText(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && addDirection()}
                placeholder="New direction..."
                className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
              />
              <select
                value={form.newPriority}
                onChange={e => form.setNewPriority(e.target.value as 'high' | 'medium' | 'low')}
                className="bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
              <select
                value={form.newCategory ?? ''}
                onChange={e => form.setNewCategory((e.target.value || null) as 'product' | 'structural' | 'mixed' | null)}
                className="bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                <option value="">Type…</option>
                <option value="product">Product</option>
                <option value="structural">Structural</option>
                <option value="mixed">Mixed</option>
              </select>
              <button
                onClick={addDirection}
                disabled={!form.newText.trim()}
                className="bg-neutral-800 hover:bg-neutral-700 disabled:opacity-30 text-neutral-300 px-3 py-1.5 min-h-[44px] sm:min-h-0 rounded text-xs transition-colors border border-neutral-700 flex items-center"
              >
                <Plus className="w-3.5 h-3.5" />
              </button>
            </div>
            <div className="mt-1.5 bg-neutral-900 border border-neutral-700 rounded px-2 py-1.5 flex flex-wrap gap-1 items-center focus-within:ring-1 focus-within:ring-neutral-600">
              {form.newPathTags.map(tag => (
                <span key={tag} className="bg-neutral-800 border border-neutral-700 text-neutral-300 text-[10px] font-mono px-1.5 py-0.5 rounded flex items-center gap-1">
                  {tag}
                  <button
                    type="button"
                    onClick={() => form.setNewPathTags(prev => prev.filter(t => t !== tag))}
                    className="text-neutral-500 hover:text-neutral-300 transition-colors leading-none"
                  >
                    <X className="w-2.5 h-2.5" />
                  </button>
                </span>
              ))}
              <input
                value={form.newPathInput}
                onChange={e => form.setNewPathInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    const val = form.newPathInput.trim();
                    if (val && !form.newPathTags.includes(val)) form.setNewPathTags(prev => [...prev, val]);
                    form.setNewPathInput('');
                  } else if (e.key === 'Backspace' && !form.newPathInput && form.newPathTags.length > 0) {
                    form.setNewPathTags(prev => prev.slice(0, -1));
                  }
                }}
                placeholder={form.newPathTags.length === 0 ? 'Path scope (e.g. backend/)' : ''}
                className="flex-1 min-w-[120px] bg-transparent text-neutral-100 text-xs focus:outline-none placeholder-neutral-600"
              />
            </div>
          </div>
        )}

        {/* Error banner */}
        {error && (
          <div className="px-5 py-2 bg-red-950/50 border-b border-red-900/50 flex items-center justify-between shrink-0">
            <span className="text-xs text-red-400">{error}</span>
            <button onClick={() => setError(null)} className="text-red-400 hover:text-red-300 transition-colors">
              <X className="w-3 h-3" />
            </button>
          </div>
        )}

        {/* Directions list */}
        <div className="flex-1 overflow-y-auto custom-scrollbar px-5 py-3">
          {/* Vision quick-link */}
          {selectedProjectId && !loading && (() => {
            const visionSystems = Array.isArray(vision.systems) ? vision.systems as Array<Record<string, unknown>> : [];
            const hasPurpose = typeof vision.purpose === 'string' && vision.purpose;
            const statusCounts = visionSystems.reduce<Record<string, number>>((acc, s) => {
              const st = String(s.status || 'planned');
              acc[st] = (acc[st] || 0) + 1;
              return acc;
            }, {});
            const vBarColors: Record<string, string> = {
              mature: 'bg-cyan-500', functional: 'bg-emerald-500', in_progress: 'bg-amber-500', planned: 'bg-neutral-600',
            };
            if (!hasPurpose && Object.keys(vision).length === 0) return null;
            return (
            <div className="mb-5">
              <button
                onClick={() => setActiveTab('vision')}
                className="flex items-center gap-2 w-full text-left group cursor-pointer hover:bg-neutral-800/50 rounded px-1.5 -mx-1.5 py-1.5 transition-colors"
              >
                <ChevronRight className="w-3.5 h-3.5 text-cyan-500/60 group-hover:text-cyan-400 transition-colors" />
                <span className="text-[12px] font-bold text-neutral-300 tracking-wide group-hover:text-neutral-100 transition-colors">
                  Product Vision
                </span>
                {typeof vision.last_updated === 'string' && (
                  <span className="text-[9px] text-neutral-600 font-mono ml-1">
                    {formatRelativeTime(vision.last_updated as string)}
                  </span>
                )}
                <span className="text-[9px] text-neutral-600 ml-1">{visionSystems.length} systems</span>
              </button>
              {visionSystems.length > 0 && (
                <div className="px-1.5 mt-0.5">
                  <div className="flex h-1.5 rounded-full overflow-hidden bg-neutral-800 mb-1">
                    {['mature', 'functional', 'in_progress', 'planned'].map(status => {
                      const count = statusCounts[status] || 0;
                      if (count === 0) return null;
                      const pct = (count / visionSystems.length) * 100;
                      return <div key={status} className={`${vBarColors[status]} transition-all`} style={{ width: `${pct}%` }} />;
                    })}
                  </div>
                </div>
              )}
            </div>
            );
          })()}

          {/* Architecture quick-link */}
          {(() => {
            const pages = Object.entries(designDoc).filter(([k]) => !k.startsWith('_'));
            const meta = (designDoc._meta || {}) as Record<string, unknown>;
            const capPages = pages.filter(([k]) => k.startsWith('capabilities/'))
              .map(([k, v]) => {
                const obj = typeof v === 'object' && v ? v as Record<string, unknown> : {};
                return { key: k, name: k.replace('capabilities/', ''), status: String(obj.status || 'missing') };
              });
            const capStatusColors: Record<string, string> = {
              mature: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
              functional: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
              partial: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
              missing: 'bg-red-500/20 text-red-400 border-red-500/30',
            };
            if (pages.length === 0) return null;
            return (
            <div className="mb-5">
              <button
                onClick={() => setActiveTab('design-doc')}
                className="flex items-center gap-2 w-full text-left group cursor-pointer hover:bg-neutral-800/50 rounded px-1.5 -mx-1.5 py-1.5 transition-colors"
              >
                <ChevronRight className="w-3.5 h-3.5 text-purple-500/60 group-hover:text-purple-400 transition-colors" />
                <span className="text-[12px] font-bold text-neutral-300 tracking-wide group-hover:text-neutral-100 transition-colors">
                  Architecture
                </span>
                {typeof meta.last_updated === 'string' && (
                  <span className="text-[9px] text-neutral-600 font-mono ml-1">
                    {formatRelativeTime(meta.last_updated as string)}
                  </span>
                )}
                <span className="text-[9px] text-neutral-600 ml-1">{pages.length} pages</span>
              </button>
              {capPages.length > 0 && (
                <div className="px-1.5 mt-0.5">
                  <div className="flex flex-wrap gap-1">
                    {capPages.slice(0, 8).map(cap => (
                      <span key={cap.key} className={`text-[8px] px-1.5 py-0.5 rounded border ${capStatusColors[cap.status] || capStatusColors.missing}`}>
                        {cap.name}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
            );
          })()}

          {/* Category filter */}
          {!loading && directions.length > 0 && (
            <div className="flex items-center gap-1 mb-3">
              {(['all', 'product', 'structural', 'mixed'] as const).map(cat => (
                <button
                  key={cat}
                  onClick={() => setCategoryFilter(cat)}
                  className={`text-[10px] px-2 py-0.5 rounded border font-medium transition-colors capitalize ${
                    categoryFilter === cat
                      ? cat === 'product' ? 'bg-emerald-950/60 border-emerald-900/50 text-emerald-400'
                        : cat === 'structural' ? 'bg-blue-950/60 border-blue-900/50 text-blue-400'
                        : cat === 'mixed' ? 'bg-purple-950/60 border-purple-900/50 text-purple-400'
                        : 'bg-neutral-700/60 border-neutral-600/50 text-neutral-300'
                      : 'bg-neutral-900/40 border-neutral-700/40 text-neutral-500 hover:text-neutral-300'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>
          )}

          {loading ? (
            <p className="text-xs text-neutral-500 text-center py-8">Loading...</p>
          ) : !selectedProjectId ? (
            <p className="text-xs text-neutral-500 text-center py-8">Select a project</p>
          ) : directions.length === 0 ? (
            <p className="text-xs text-neutral-500 text-center py-8">No directions yet</p>
          ) : (
            <div className="space-y-4">
              {grouped.map(({ key, label, items }) => {
                if (items.length === 0) return null;
                const isCollapsed = collapsed[key] ?? false;
                return (
                  <div key={key}>
                    <button
                      onClick={() => toggleSection(key)}
                      className="flex items-center gap-1.5 w-full text-left mb-2 group"
                    >
                      {isCollapsed
                        ? <ChevronRight className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                        : <ChevronDown className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                      }
                      <span className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider group-hover:text-neutral-300 transition-colors">
                        {label} ({items.length})
                      </span>
                    </button>
                    {!isCollapsed && (
                      <div className="space-y-2">
                        {items.map(d => (
                          <DirectionCard
                            key={d.id}
                            direction={d}
                            statusKey={key}
                            activeItems={activeItems}
                            editingId={form.editingId}
                            editText={form.editText}
                            editPriority={form.editPriority}
                            editStatus={form.editStatus}
                            editCategory={form.editCategory}
                            editPathTags={form.editPathTags}
                            editPathInput={form.editPathInput}
                            setEditingId={form.setEditingId}
                            setEditText={form.setEditText}
                            setEditPriority={form.setEditPriority}
                            setEditStatus={form.setEditStatus}
                            setEditCategory={form.setEditCategory}
                            setEditPathTags={form.setEditPathTags}
                            setEditPathInput={form.setEditPathInput}
                            startEdit={form.startEdit}
                            updateDirection={updateDirection}
                            approveProposal={approveProposal}
                            rejectProposal={rejectProposal}
                            reviveProposal={reviveProposal}
                            reactivateDirection={reactivateDirection}
                            markDirectionComplete={markDirectionComplete}
                            quickToggleStatus={quickToggleStatus}
                            deleteDirection={deleteDirection}
                            applyDirectionUpdate={applyDirectionUpdate}
                            dismissDirectionUpdate={dismissDirectionUpdate}
                            triggerResearch={triggerResearch}
                            reorderDirections={reorderDirections}
                            taskCounts={taskCounts}
                            activeTasks={activeTasks}
                            cooldownMinutes={config.cooldownMinutes}
                            researchingIds={researchingIds}
                            researchSuccessIds={researchSuccessIds}
                            forceConfirmId={forceConfirmId}
                            setForceConfirmId={setForceConfirmId}
                            historyOpenIds={historyOpenIds}
                            setHistoryOpenIds={setHistoryOpenIds}
                            onViewFindings={onViewFindings}
                            onNavigateToTask={onNavigateToTask}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
        </>)}

        {/* === Vision tab content === */}
        {activeTab === 'vision' && (
          <div className="flex-1 overflow-y-auto custom-scrollbar">
            <VisionViewer
              vision={vision}
              directions={directions}
              selectedProjectId={selectedProjectId}
              addToast={addToast}
              onVisionUpdated={setVision}
            />
          </div>
        )}

        {/* === Design Doc tab content === */}
        {activeTab === 'design-doc' && (
          <div className="flex-1 overflow-hidden">
            <DesignDocViewer designDoc={designDoc} />
          </div>
        )}
      </div>
    </div>
  );
}
