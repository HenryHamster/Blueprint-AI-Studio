import { useState, useEffect, useRef } from 'react';
import { Plus, Pencil, Trash2, Check, X, ToggleLeft, ToggleRight, ChevronDown, ChevronRight, ChevronUp, Settings, Zap } from 'lucide-react';
import { API_BASE_URL, authHeaders } from '../api';
import { VisionEditorModal } from './VisionEditorModal';

interface Project {
  id: string;
  name?: string;
  [key: string]: unknown;
}

interface ResearchHistoryEntry {
  timestamp: string;
  task_id: string;
  tasks_created: number;
  finding_titles: string[];
  key_finding_descriptions: string[];
  tasks_completed?: number;
  tasks_failed?: number;
  cycle_insights?: string[];
}

interface Direction {
  id: string;
  text: string;
  priority: 'high' | 'medium' | 'low';
  added_at: string;
  status: 'active' | 'completed' | 'paused' | 'proposed' | 'expired';
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
}

function formatRelativeTime(isoString: string): string {
  const diffMs = Date.now() - new Date(isoString.endsWith('Z') ? isoString : isoString + 'Z').getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHrs = Math.floor(diffMins / 60);
  if (diffHrs < 24) return `${diffHrs}h ${diffMins % 60}m ago`;
  return `${Math.floor(diffHrs / 24)}d ago`;
}

function formatTimeRemaining(isoString: string, cooldownMinutes: number): string {
  // Ensure UTC interpretation — backend stores naive UTC timestamps without 'Z' suffix
  const elapsedMs = Date.now() - new Date(isoString.endsWith('Z') ? isoString : isoString + 'Z').getTime();
  const remainingMs = cooldownMinutes * 60 * 1000 - elapsedMs;
  if (remainingMs <= 0) return '';
  const remainingMins = Math.ceil(remainingMs / 60000);
  if (remainingMins < 60) return `${remainingMins}m left`;
  const hrs = Math.floor(remainingMins / 60);
  const mins = remainingMins % 60;
  return mins > 0 ? `${hrs}h ${mins}m left` : `${hrs}h left`;
}

function getCooldownStatus(d: Direction, cooldownMinutes: number): 'ready' | 'cooling' | 'never' {
  if (!d.last_researched_at) return 'never';
  const ts = d.last_researched_at;
  const elapsedMs = Date.now() - new Date(ts.endsWith('Z') ? ts : ts + 'Z').getTime();
  return elapsedMs < cooldownMinutes * 60 * 1000 ? 'cooling' : 'ready';
}

const priorityColors: Record<string, string> = {
  high: 'bg-red-950/40 text-red-400 border-red-900/50',
  medium: 'bg-amber-950/40 text-amber-400 border-amber-900/50',
  low: 'bg-blue-950/40 text-blue-400 border-blue-900/50',
};

const statusColors: Record<string, string> = {
  active: 'bg-emerald-950/40 text-emerald-400 border-emerald-900/50',
  completed: 'bg-neutral-800/40 text-neutral-400 border-neutral-700/50',
  paused: 'bg-amber-950/40 text-amber-400 border-amber-900/50',
  proposed: 'bg-purple-950/40 text-purple-400 border-purple-900/50',
  expired: 'bg-neutral-800/40 text-neutral-500 border-neutral-700/50',
};

const priorityOrder: Record<string, number> = { high: 0, medium: 1, low: 2 };

const STATUS_GROUPS: Array<{ key: 'proposed' | 'active' | 'paused' | 'completed' | 'expired'; label: string }> = [
  { key: 'proposed', label: 'Proposed by Agent' },
  { key: 'active', label: 'Active' },
  { key: 'paused', label: 'Paused' },
  { key: 'completed', label: 'Completed' },
  { key: 'expired', label: 'Expired' },
];

export function DirectionsPanel({ onClose, addToast }: { onClose: () => void; addToast: (message: string, type: 'success' | 'error' | 'info') => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [directions, setDirections] = useState<Direction[]>([]);
  const [autonomousEnabled, setAutonomousEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [taskCounts, setTaskCounts] = useState<Record<string, number>>({});
  const [vision, setVision] = useState<Record<string, unknown>>({});
  const [visionExpanded, setVisionExpanded] = useState(false);
  const [designDoc, setDesignDoc] = useState<Record<string, unknown>>({});
  const [designDocExpanded, setDesignDocExpanded] = useState(false);
  const [showVisionEditor, setShowVisionEditor] = useState(false);
  const [showStrategicOutput, setShowStrategicOutput] = useState(false);
  const [strategicOutput, setStrategicOutput] = useState<Record<string, unknown> | null>(null);
  const [strategicTaskId, setStrategicTaskId] = useState<string | null>(null);
  const [loadingStrategic, setLoadingStrategic] = useState(false);
  const [contextExpanded, setContextExpanded] = useState(false);

  // Add form
  const [newText, setNewText] = useState('');
  const [newPriority, setNewPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [newPathTags, setNewPathTags] = useState<string[]>([]);
  const [newPathInput, setNewPathInput] = useState('');

  // Error feedback
  const [error, setError] = useState<string | null>(null);

  // Inline edit
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [editPriority, setEditPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [editStatus, setEditStatus] = useState<'active' | 'completed' | 'paused' | 'proposed' | 'expired'>('active');
  const [editPathTags, setEditPathTags] = useState<string[]>([]);
  const [editPathInput, setEditPathInput] = useState('');

  // Collapsible sections — expired collapsed by default, others expanded
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({ expired: true });

  // Research history open state per direction
  const [historyOpenIds, setHistoryOpenIds] = useState<Record<string, boolean>>({});

  // Autonomous config settings (global)
  const [configCooldownHours, setConfigCooldownHours] = useState<number>(24);
  const [configIntervalMinutes, setConfigIntervalMinutes] = useState<number>(60);
  const [configWaveSize, setConfigWaveSize] = useState<number>(3);
  const [configMaxTasksPerHour, setConfigMaxTasksPerHour] = useState<number>(10);
  const [autonomousConfigOpen, setAutonomousConfigOpen] = useState(false);
  const [savingConfigField, setSavingConfigField] = useState<string | null>(null);

  // Research button state
  const [researchingIds, setResearchingIds] = useState<Record<string, boolean>>({});
  const [researchSuccessIds, setResearchSuccessIds] = useState<Record<string, boolean>>({});

  // Cooldown settings
  const [cooldownMinutes, setCooldownMinutes] = useState<number>(240);

  // Force-research confirmation popover
  const [forceConfirmId, setForceConfirmId] = useState<string | null>(null);
  const forceConfirmRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    fetchProjects();
    fetchCooldownSetting();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Close force-confirm popover on outside click
  useEffect(() => {
    if (!forceConfirmId) return;
    function handleClick(e: MouseEvent) {
      if (forceConfirmRef.current && !forceConfirmRef.current.contains(e.target as Node)) {
        setForceConfirmId(null);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [forceConfirmId]);

  useEffect(() => {
    if (selectedProjectId) {
      fetchDirections();
      fetchSettings();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  useEffect(() => {
    if (!showStrategicOutput || !selectedProjectId) return;
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
  }, [showStrategicOutput, selectedProjectId]);

  async function fetchCooldownSetting() {
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, { headers: h });
      if (!res.ok) return;
      const data = await res.json();
      const val = data.autonomous_direction_cooldown_minutes;
      if (typeof val === 'number' && val > 0) setCooldownMinutes(val);
    } catch (err) {
      console.error('Failed to fetch cooldown setting:', err);
      addToast('Failed to load cooldown settings', 'error');
    }
  }

  async function fetchProjects() {
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects`, { headers: h });
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

  async function fetchSettings() {
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, { headers: h });
      if (!res.ok) return;
      const data = await res.json();
      if (data.autonomous_direction_cooldown_minutes != null) setConfigCooldownHours(data.autonomous_direction_cooldown_minutes / 60);
      if (data.autonomous_research_interval_minutes != null) setConfigIntervalMinutes(data.autonomous_research_interval_minutes);
      if (data.autonomous_research_wave_size != null) setConfigWaveSize(data.autonomous_research_wave_size);
      if (data.max_autonomous_tasks_per_hour != null) setConfigMaxTasksPerHour(data.max_autonomous_tasks_per_hour);
    } catch { /* ignore */ }
  }

  async function saveConfigField(field: string, value: number) {
    setSavingConfigField(field);
    setError(null);
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: h,
        body: JSON.stringify({ [field]: value }),
      });
      if (!res.ok) throw new Error('Failed to save setting');
    } catch {
      setError('Failed to save setting. Please try again.');
    } finally {
      setSavingConfigField(null);
    }
  }

  async function fetchDirections() {
    setLoading(true);
    try {
      const h = await authHeaders();
      const [res, countsRes] = await Promise.all([
        fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions`, { headers: h }),
        fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/task-counts`, { headers: h }),
      ]);
      if (!res.ok) throw new Error('Failed to fetch directions');
      const data = await res.json();
      setDirections(data.directions ?? []);
      setAutonomousEnabled(data.autonomous_enabled ?? false);
      setVision(data.vision ?? {});
      setDesignDoc(data.design_doc ?? {});
      if ((data.vision ?? {}).purpose) setVisionExpanded(true);
      if (countsRes.ok) {
        const countsData = await countsRes.json();
        setTaskCounts(countsData.counts ?? {});
      }
    } catch (err) {
      console.error('Failed to fetch directions:', err);
      addToast('Failed to load directions', 'error');
    }
    setLoading(false);
  }

  async function addDirection() {
    if (!newText.trim() || !selectedProjectId) return;
    setError(null);
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions`, {
        method: 'POST', headers: h,
        body: JSON.stringify({ text: newText.trim(), priority: newPriority, paths: newPathTags }),
      });
      if (!res.ok) throw new Error('Failed to add direction');
      setNewText('');
      setNewPriority('medium');
      setNewPathTags([]);
      setNewPathInput('');
      fetchDirections();
    } catch {
      setError('Failed to add direction. Please try again.');
    }
  }

  async function updateDirection(id: string) {
    setError(null);
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH', headers: h,
        body: JSON.stringify({ text: editText, priority: editPriority, status: editStatus, paths: editPathTags }),
      });
      if (!res.ok) throw new Error('Failed to update direction');
      setEditingId(null);
      fetchDirections();
    } catch {
      setError('Failed to update direction. Please try again.');
    }
  }

  async function quickToggleStatus(id: string, currentStatus: 'active' | 'paused') {
    setError(null);
    const newStatus = currentStatus === 'active' ? 'paused' : 'active';
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH', headers: h,
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
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH', headers: { ...h, 'Content-Type': 'application/json' },
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
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH', headers: { ...h, 'Content-Type': 'application/json' },
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
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH', headers: h,
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
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'DELETE', headers: h,
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
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH', headers: { ...h, 'Content-Type': 'application/json' },
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
      const h = await authHeaders();
      const updated: Record<string, unknown> = { pending_update: null };
      if (d.pending_update.suggested_text) updated.text = d.pending_update.suggested_text;
      if (d.pending_update.suggested_priority) updated.priority = d.pending_update.suggested_priority;
      if (d.pending_update.suggested_paths) updated.paths = d.pending_update.suggested_paths;
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${d.id}`, {
        method: 'PATCH', headers: h,
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
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH', headers: h,
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
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'DELETE', headers: h,
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
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/autonomous`, {
        method: 'PATCH', headers: h,
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
    // Optimistic update: replace directions with new order (non-active directions stay in place)
    setDirections(prev => {
      const nonActive = prev.filter(d => d.status !== 'active');
      return [...newOrder, ...nonActive];
    });
    setError(null);
    try {
      const h = await authHeaders();
      // Full reorder: active in new order, then non-active (proposed/paused/completed) in original order
      const nonActive = previousDirections.filter(d => d.status !== 'active');
      const allOrdered = [...newOrder, ...nonActive];
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/reorder`, {
        method: 'PATCH', headers: h,
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
      const h = await authHeaders();
      const url = `${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions/${id}/research${bypass ? '?bypass_cooldown=true' : ''}`;
      const res = await fetch(url, { method: 'POST', headers: h });
      if (!res.ok) throw new Error('Failed to trigger research');
      setResearchSuccessIds(prev => ({ ...prev, [id]: true }));
      setTimeout(() => setResearchSuccessIds(prev => ({ ...prev, [id]: false })), 3000);
    } catch {
      setError('Failed to trigger research. Please try again.');
    } finally {
      setResearchingIds(prev => ({ ...prev, [id]: false }));
    }
  }

  function startEdit(d: Direction) {
    setEditingId(d.id);
    setEditText(d.text);
    setEditPriority(d.priority);
    setEditStatus(d.status);
    setEditPathTags(d.paths ?? []);
    setEditPathInput('');
  }

  function toggleSection(key: string) {
    setCollapsed(prev => ({ ...prev, [key]: !prev[key] }));
  }

  const grouped = STATUS_GROUPS.map(({ key, label }) => ({
    key,
    label,
    // Active directions preserve user-defined order; others sort by priority
    items: key === 'active'
      ? directions.filter(d => d.status === key)
      : directions
          .filter(d => d.status === key)
          .sort((a, b) => priorityOrder[a.priority] - priorityOrder[b.priority]),
  }));

  const proposedCount = directions.filter(d => d.status === 'proposed').length;

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />

      {/* Panel */}
      <div className="relative ml-auto w-full sm:max-w-lg h-full bg-neutral-950 border-l border-neutral-800 flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-neutral-800 shrink-0">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold text-neutral-100 uppercase tracking-wider">Directions</h2>
            {proposedCount > 0 && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full font-semibold bg-purple-950/60 text-purple-400 border border-purple-900/50">
                {proposedCount} pending
              </span>
            )}
          </div>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Project selector + Autonomous toggle */}
        <div className="px-5 py-3 border-b border-neutral-800 space-y-3 shrink-0">
          <div>
            <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Project</label>
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

              {/* Autonomous Config collapsible */}
              <div>
                <button
                  onClick={() => setAutonomousConfigOpen(v => !v)}
                  className="flex items-center gap-1.5 w-full text-left group"
                >
                  <Settings className="w-3 h-3 text-neutral-500 group-hover:text-neutral-400 transition-colors" />
                  <span className="text-[11px] font-medium text-neutral-500 uppercase tracking-wider group-hover:text-neutral-400 transition-colors flex-1">
                    Autonomous Config
                  </span>
                  {autonomousConfigOpen
                    ? <ChevronDown className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                    : <ChevronRight className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                  }
                </button>

                {autonomousConfigOpen && (
                  <div className="mt-2 space-y-2">
                    {/* Cooldown (hours) */}
                    <div className="flex items-center gap-2">
                      <label className="text-[11px] text-neutral-400 w-32 shrink-0">Cooldown (hours)</label>
                      <input
                        type="number"
                        min={1}
                        max={720}
                        value={configCooldownHours}
                        onChange={e => setConfigCooldownHours(Number(e.target.value))}
                        className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                      />
                      <button
                        onClick={() => saveConfigField('autonomous_direction_cooldown_hours', configCooldownHours)}
                        disabled={savingConfigField === 'autonomous_direction_cooldown_hours'}
                        className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
                        title="Save"
                      >
                        <Check className="w-3.5 h-3.5" />
                      </button>
                    </div>

                    {/* Poll interval (min) */}
                    <div className="flex items-center gap-2">
                      <label className="text-[11px] text-neutral-400 w-32 shrink-0">Poll interval (min)</label>
                      <input
                        type="number"
                        min={5}
                        max={1440}
                        value={configIntervalMinutes}
                        onChange={e => setConfigIntervalMinutes(Number(e.target.value))}
                        className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                      />
                      <button
                        onClick={() => saveConfigField('autonomous_research_interval_minutes', configIntervalMinutes)}
                        disabled={savingConfigField === 'autonomous_research_interval_minutes'}
                        className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
                        title="Save"
                      >
                        <Check className="w-3.5 h-3.5" />
                      </button>
                    </div>

                    {/* Wave size */}
                    <div className="flex items-center gap-2">
                      <label className="text-[11px] text-neutral-400 w-32 shrink-0">Wave size</label>
                      <input
                        type="number"
                        min={1}
                        max={50}
                        value={configWaveSize}
                        onChange={e => setConfigWaveSize(Number(e.target.value))}
                        className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                      />
                      <button
                        onClick={() => saveConfigField('autonomous_research_wave_size', configWaveSize)}
                        disabled={savingConfigField === 'autonomous_research_wave_size'}
                        className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
                        title="Save"
                      >
                        <Check className="w-3.5 h-3.5" />
                      </button>
                    </div>

                    {/* Max tasks/hour */}
                    <div className="flex items-center gap-2">
                      <label className="text-[11px] text-neutral-400 w-32 shrink-0">Max tasks/hour</label>
                      <input
                        type="number"
                        min={1}
                        max={50}
                        value={configMaxTasksPerHour}
                        onChange={e => setConfigMaxTasksPerHour(Number(e.target.value))}
                        className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                      />
                      <button
                        onClick={() => saveConfigField('max_autonomous_tasks_per_hour', configMaxTasksPerHour)}
                        disabled={savingConfigField === 'max_autonomous_tasks_per_hour'}
                        className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
                        title="Save"
                      >
                        <Check className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        {/* Add direction form */}
        {selectedProjectId && (
          <div className="px-5 py-3 border-b border-neutral-800 shrink-0">
            <div className="flex gap-2">
              <input
                value={newText}
                onChange={e => setNewText(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && addDirection()}
                placeholder="New direction..."
                className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
              />
              <select
                value={newPriority}
                onChange={e => setNewPriority(e.target.value as 'high' | 'medium' | 'low')}
                className="bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
              <button
                onClick={addDirection}
                disabled={!newText.trim()}
                className="bg-neutral-800 hover:bg-neutral-700 disabled:opacity-30 text-neutral-300 px-3 py-1.5 min-h-[44px] sm:min-h-0 rounded text-xs transition-colors border border-neutral-700 flex items-center"
              >
                <Plus className="w-3.5 h-3.5" />
              </button>
            </div>
            <div className="mt-1.5 bg-neutral-900 border border-neutral-700 rounded px-2 py-1.5 flex flex-wrap gap-1 items-center focus-within:ring-1 focus-within:ring-neutral-600">
              {newPathTags.map(tag => (
                <span key={tag} className="bg-neutral-800 border border-neutral-700 text-neutral-300 text-[10px] font-mono px-1.5 py-0.5 rounded flex items-center gap-1">
                  {tag}
                  <button
                    type="button"
                    onClick={() => setNewPathTags(prev => prev.filter(t => t !== tag))}
                    className="text-neutral-500 hover:text-neutral-300 transition-colors leading-none"
                  >
                    <X className="w-2.5 h-2.5" />
                  </button>
                </span>
              ))}
              <input
                value={newPathInput}
                onChange={e => setNewPathInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' || e.key === ',') {
                    e.preventDefault();
                    const val = newPathInput.trim();
                    if (val && !newPathTags.includes(val)) setNewPathTags(prev => [...prev, val]);
                    setNewPathInput('');
                  } else if (e.key === 'Backspace' && !newPathInput && newPathTags.length > 0) {
                    setNewPathTags(prev => prev.slice(0, -1));
                  }
                }}
                placeholder={newPathTags.length === 0 ? 'Path scope (e.g. backend/)' : ''}
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
          {/* Vision — strategic layer */}
          {selectedProjectId && !loading && (() => {
            const hasPurpose = typeof vision.purpose === 'string' && vision.purpose;
            const visionSystems = Array.isArray(vision.systems) ? vision.systems as Array<Record<string, unknown>> : [];
            const statusCounts = visionSystems.reduce<Record<string, number>>((acc, s) => {
              const st = String(s.status || 'planned');
              acc[st] = (acc[st] || 0) + 1;
              return acc;
            }, {});
            const barColors: Record<string, string> = {
              mature: 'bg-cyan-500', functional: 'bg-emerald-500', in_progress: 'bg-amber-500', planned: 'bg-neutral-600',
            };
            const visionStatusColors: Record<string, string> = {
              planned: 'bg-neutral-700 text-neutral-400',
              in_progress: 'bg-amber-500/20 text-amber-400',
              functional: 'bg-emerald-500/20 text-emerald-400',
              mature: 'bg-cyan-500/20 text-cyan-400',
            };
            return (
            <div className="mb-5">
              {/* Header */}
              <div className="flex items-center gap-2 mb-1.5">
                <button
                  onClick={() => setVisionExpanded(prev => !prev)}
                  className="flex items-center gap-2 flex-1 text-left group cursor-pointer hover:bg-neutral-800/50 rounded px-1.5 -mx-1.5 py-1 transition-colors"
                >
                  {visionExpanded
                    ? <ChevronDown className="w-3.5 h-3.5 text-cyan-500/60 group-hover:text-cyan-400 transition-colors" />
                    : <ChevronRight className="w-3.5 h-3.5 text-cyan-500/60 group-hover:text-cyan-400 transition-colors" />
                  }
                  <span className="text-[12px] font-bold text-neutral-300 tracking-wide group-hover:text-neutral-100 transition-colors">
                    Product Vision
                  </span>
                  {typeof vision.last_updated === 'string' && (
                    <span className="text-[9px] text-neutral-600 font-mono ml-1">
                      {formatRelativeTime(vision.last_updated as string)}
                    </span>
                  )}
                </button>
                <button
                  onClick={() => setShowVisionEditor(true)}
                  className="text-neutral-600 hover:text-neutral-400 transition-colors p-1 rounded hover:bg-neutral-800/50"
                  title="Edit vision"
                >
                  <Pencil className="w-3 h-3" />
                </button>
              </div>

              {/* Collapsed: systems health bar */}
              {!visionExpanded && visionSystems.length > 0 && (
                <div className="px-1.5">
                  <div className="flex h-1.5 rounded-full overflow-hidden bg-neutral-800 mb-1">
                    {['mature', 'functional', 'in_progress', 'planned'].map(status => {
                      const count = statusCounts[status] || 0;
                      if (count === 0) return null;
                      const pct = (count / visionSystems.length) * 100;
                      return <div key={status} className={`${barColors[status]} transition-all`} style={{ width: `${pct}%` }} />;
                    })}
                  </div>
                  <div className="flex items-center gap-2 text-[9px] text-neutral-600">
                    <span>{visionSystems.length} systems</span>
                    {statusCounts.mature ? <span className="text-cyan-500/70">{statusCounts.mature} mature</span> : null}
                    {statusCounts.functional ? <span className="text-emerald-500/70">{statusCounts.functional} functional</span> : null}
                    {statusCounts.in_progress ? <span className="text-amber-500/70">{statusCounts.in_progress} in progress</span> : null}
                    {statusCounts.planned ? <span className="text-neutral-500">{statusCounts.planned} planned</span> : null}
                  </div>
                </div>
              )}

              {/* Empty state */}
              {visionExpanded && !hasPurpose && Object.keys(vision).length === 0 && (
                <div className="bg-neutral-900/60 border border-neutral-800 border-dashed rounded-lg p-4 text-center">
                  <p className="text-xs text-neutral-500 mb-2">No vision set yet. Describe what this project should build.</p>
                  <button
                    onClick={() => setShowVisionEditor(true)}
                    className="text-xs text-cyan-400 hover:text-cyan-300 transition-colors font-medium"
                  >
                    Set initial vision
                  </button>
                </div>
              )}

              {/* Expanded: full vision */}
              {visionExpanded && (hasPurpose || Object.keys(vision).length > 0) && (() => {
                const purpose = typeof vision.purpose === 'string' ? vision.purpose : '';
                const criteria = Array.isArray(vision.success_criteria) ? vision.success_criteria as string[] : [];
                const context = typeof vision.context === 'string' ? vision.context : '';
                return (
                <div className="border-l-2 border-cyan-500/20 pl-3 ml-1 space-y-2.5 mt-1">
                  {purpose && (
                    <p className="text-xs text-neutral-300 leading-relaxed">{purpose}</p>
                  )}
                  {criteria.length > 0 && (
                    <div>
                      <span className="text-[10px] font-semibold text-neutral-500 uppercase">Success Criteria</span>
                      <ul className="mt-0.5 space-y-0.5">
                        {criteria.map((c, i) => (
                          <li key={i} className="text-xs text-neutral-400">- {String(c)}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {visionSystems.length > 0 && (
                    <div>
                      <span className="text-[10px] font-semibold text-neutral-500 uppercase cursor-help" title="planned = not started, in progress = being built, functional = working, mature = stable">Systems</span>
                      {/* Progress bar */}
                      <div className="flex h-1.5 rounded-full overflow-hidden bg-neutral-800 mt-1.5 mb-2">
                        {['mature', 'functional', 'in_progress', 'planned'].map(status => {
                          const count = statusCounts[status] || 0;
                          if (count === 0) return null;
                          const pct = (count / visionSystems.length) * 100;
                          return <div key={status} className={`${barColors[status]} transition-all`} style={{ width: `${pct}%` }} />;
                        })}
                      </div>
                      <div className="space-y-1">
                        {visionSystems.map((sys, sysIdx) => {
                          const sysStatus = String(sys.status || 'planned');
                          const color = visionStatusColors[sysStatus] || visionStatusColors.planned;
                          const desc = typeof sys.description === 'string' ? sys.description : '';
                          return (
                            <div key={String(sys.id || `sys-${sysIdx}`)} className="flex items-center gap-2 text-xs">
                              <span className={`px-1.5 py-0.5 rounded text-[9px] font-medium border border-transparent ${color}`}>
                                {sysStatus.replace('_', ' ')}
                              </span>
                              <span className="text-neutral-300 font-medium">{String(sys.name)}</span>
                              {desc && <span className="text-neutral-600 truncate">— {desc}</span>}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                  {context && (
                    <div>
                      <span className="text-[10px] font-semibold text-neutral-500 uppercase">Context</span>
                      <p className="text-xs text-neutral-500 mt-0.5 whitespace-pre-wrap">
                        {contextExpanded ? context : context.slice(0, 500)}{!contextExpanded && context.length > 500 && '...'}
                      </p>
                      {context.length > 500 && (
                        <button
                          onClick={() => setContextExpanded(prev => !prev)}
                          className="text-[10px] text-cyan-500 hover:text-cyan-400 transition-colors mt-0.5"
                        >
                          {contextExpanded ? 'Show less' : 'Show more'}
                        </button>
                      )}
                    </div>
                  )}
                  <div className="flex items-center justify-between text-[10px] pt-1.5 border-t border-neutral-800/50">
                    <span className="text-neutral-600">
                      Guiding {directions.filter(d => d.status === 'active').length} active direction{directions.filter(d => d.status === 'active').length !== 1 ? 's' : ''}
                    </span>
                    <button
                      onClick={() => setShowStrategicOutput(true)}
                      className="text-cyan-500 hover:text-cyan-400 transition-colors underline underline-offset-2"
                    >
                      View last strategic review
                    </button>
                  </div>
                </div>
                );
              })()}
              {showStrategicOutput && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => setShowStrategicOutput(false)}>
                  <div className="bg-neutral-900 border border-neutral-700 rounded-xl p-5 max-w-2xl w-full mx-4 shadow-2xl max-h-[80vh] overflow-y-auto custom-scrollbar" onClick={e => e.stopPropagation()}>
                    <div className="flex justify-between items-center mb-4">
                      <h3 className="text-sm font-semibold text-neutral-100">Last Strategic Review</h3>
                      <button onClick={() => setShowStrategicOutput(false)} className="text-neutral-500 hover:text-neutral-300 transition-colors"><X className="w-4 h-4" /></button>
                    </div>
                    {loadingStrategic ? (
                      <p className="text-xs text-neutral-500">Loading...</p>
                    ) : strategicOutput ? (() => {
                      const sVision = strategicOutput.vision as Record<string, unknown> | undefined;
                      const sDirOps = Array.isArray(strategicOutput.direction_operations) ? strategicOutput.direction_operations as Array<Record<string, string>> : [];
                      return (
                        <div className="space-y-4">
                          {/* Vision summary */}
                          {sVision && (
                            <div>
                              <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Vision Update</h4>
                              {typeof sVision.purpose === 'string' && (
                                <p className="text-xs text-neutral-300 mb-1.5">{sVision.purpose}</p>
                              )}
                              {Array.isArray(sVision.systems) && (
                                <div className="space-y-1 mt-2">
                                  {(sVision.systems as Array<Record<string, string>>).map((sys) => {
                                    const sColors: Record<string, string> = {
                                      planned: 'text-neutral-400', in_progress: 'text-amber-400',
                                      functional: 'text-emerald-400', mature: 'text-cyan-400',
                                    };
                                    return (
                                      <div key={sys.id} className="flex items-center gap-2 text-xs">
                                        <span className={sColors[sys.status] || 'text-neutral-400'}>{(sys.status || 'planned').replace('_', ' ')}</span>
                                        <span className="text-neutral-300">{sys.name}</span>
                                        {sys.description && <span className="text-neutral-600">— {sys.description}</span>}
                                      </div>
                                    );
                                  })}
                                </div>
                              )}
                              {typeof sVision.context === 'string' && (
                                <p className="text-[11px] text-neutral-500 mt-2 whitespace-pre-wrap">{(sVision.context as string).slice(0, 800)}</p>
                              )}
                            </div>
                          )}
                          {/* Direction operations */}
                          {sDirOps.length > 0 && (
                            <div>
                              <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Direction Changes ({sDirOps.length})</h4>
                              <div className="space-y-2">
                                {sDirOps.map((op, i) => {
                                  const opColors: Record<string, string> = {
                                    create: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
                                    update: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
                                    retire: 'text-red-400 bg-red-500/10 border-red-500/20',
                                  };
                                  const color = opColors[op.op] || 'text-neutral-400 bg-neutral-700 border-neutral-600';
                                  return (
                                    <div key={i} className="bg-neutral-950 border border-neutral-800 rounded-lg p-2.5">
                                      <div className="flex items-center gap-2 mb-1">
                                        <span className={`text-[9px] font-semibold uppercase px-1.5 py-0.5 rounded border ${color}`}>{op.op}</span>
                                        {op.text && <span className="text-xs text-neutral-200 flex-1">{op.text}</span>}
                                        {op.op === 'update' && op.changes && (() => {
                                          const c = op.changes as unknown as Record<string, string>;
                                          const parts = [
                                            c.text && `text \u2192 "${c.text}"`,
                                            c.priority && `priority \u2192 ${c.priority}`,
                                            c.status && `status \u2192 ${c.status}`,
                                          ].filter(Boolean);
                                          return parts.length > 0 ? (
                                            <span className="text-xs text-neutral-300 flex-1">{parts.join(', ')}</span>
                                          ) : null;
                                        })()}
                                        {op.direction_id && <span className="text-[9px] text-neutral-600 font-mono">{op.direction_id}</span>}
                                      </div>
                                      {op.rationale && <p className="text-[11px] text-neutral-500">{op.rationale}</p>}
                                      {op.priority && <span className="text-[9px] text-neutral-600">Priority: {op.priority}</span>}
                                    </div>
                                  );
                                })}
                              </div>
                            </div>
                          )}
                          {!sVision && sDirOps.length === 0 && (
                            <p className="text-xs text-neutral-500">No structured output — the review may have failed or produced an unexpected format.</p>
                          )}
                        </div>
                      );
                    })() : (
                      <p className="text-xs text-neutral-500">No completed strategic reviews found yet. Strategic reviews run every Nth research cycle (configurable in Settings).</p>
                    )}
                    {strategicTaskId && (
                      <p className="text-[10px] text-neutral-600 mt-3 pt-2 border-t border-neutral-800">Task: {strategicTaskId}</p>
                    )}
                  </div>
                </div>
              )}
              {showVisionEditor && selectedProjectId && (
                <VisionEditorModal
                  projectId={selectedProjectId}
                  initialVision={vision}
                  onSave={(updatedVision) => {
                    setVision(updatedVision);
                    setShowVisionEditor(false);
                    addToast('Vision updated', 'success');
                  }}
                  onClose={() => setShowVisionEditor(false)}
                />
              )}
            </div>
            );
          })()}

          {/* ── Design Document (Architecture) — page-based wiki ───────── */}
          {(() => {
            // Extract pages from the flat key-value design doc (skip _meta)
            const pages = Object.entries(designDoc).filter(([k]) => !k.startsWith('_'));
            const meta = (designDoc._meta || {}) as Record<string, unknown>;
            const docSummary = typeof designDoc.summary === 'string' ? designDoc.summary as string : '';
            const docArch = (typeof designDoc.architecture === 'object' && designDoc.architecture && !Array.isArray(designDoc.architecture))
              ? designDoc.architecture as Record<string, unknown> : null;

            // Group pages by prefix
            const systemPages = pages.filter(([k]) => k.startsWith('systems/'))
              .map(([k, v]) => {
                const obj = typeof v === 'object' && v ? v as Record<string, unknown> : {};
                return { key: k, id: String(obj.id || k), name: String(obj.name || k.replace('systems/', '')), status: String(obj.status || 'missing'), description: typeof obj.description === 'string' ? obj.description : '' };
              });
            const capPages = pages.filter(([k]) => k.startsWith('capabilities/'))
              .map(([k, v]) => {
                const obj = typeof v === 'object' && v ? v as Record<string, unknown> : {};
                return { key: k, name: k.replace('capabilities/', ''), status: String(obj.status || 'missing'), notes: typeof obj.notes === 'string' ? obj.notes : '' };
              });
            const risksPage = designDoc.risks;
            const docRisks = Array.isArray(risksPage) ? risksPage as string[] : [];
            const otherPages = pages.filter(([k]) => !k.startsWith('systems/') && !k.startsWith('capabilities/') && k !== 'summary' && k !== 'architecture' && k !== 'risks');

            const hasContent = pages.length > 0;

            const capStatusColors: Record<string, string> = {
              mature: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
              functional: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
              partial: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
              missing: 'bg-red-500/20 text-red-400 border-red-500/30',
            };
            const sysStatusColors: Record<string, string> = {
              mature: 'text-cyan-400', functional: 'text-emerald-400',
              partial: 'text-amber-400', stub: 'text-orange-400', missing: 'text-red-400',
            };

            if (!hasContent && !designDocExpanded) return null;

            return (
            <div className="mb-5">
              <div className="flex items-center gap-2 mb-1.5">
                <button
                  onClick={() => setDesignDocExpanded(prev => !prev)}
                  className="flex items-center gap-2 flex-1 text-left group cursor-pointer hover:bg-neutral-800/50 rounded px-1.5 -mx-1.5 py-1 transition-colors"
                >
                  {designDocExpanded
                    ? <ChevronDown className="w-3.5 h-3.5 text-purple-500/60 group-hover:text-purple-400 transition-colors" />
                    : <ChevronRight className="w-3.5 h-3.5 text-purple-500/60 group-hover:text-purple-400 transition-colors" />
                  }
                  <span className="text-[12px] font-bold text-neutral-300 tracking-wide group-hover:text-neutral-100 transition-colors">
                    Architecture
                  </span>
                  {typeof meta.last_updated === 'string' && (
                    <span className="text-[9px] text-neutral-600 font-mono ml-1">
                      {formatRelativeTime(meta.last_updated as string)}
                    </span>
                  )}
                  {!designDocExpanded && (
                    <span className="text-[9px] text-neutral-600 ml-1">{pages.length} pages</span>
                  )}
                </button>
              </div>

              {/* Collapsed: capability badges */}
              {!designDocExpanded && capPages.length > 0 && (
                <div className="px-1.5">
                  <div className="flex flex-wrap gap-1">
                    {capPages.slice(0, 8).map(cap => {
                      const status = String(cap.status || 'missing');
                      return (
                        <span key={cap.key} className={`text-[8px] px-1.5 py-0.5 rounded border ${capStatusColors[status] || capStatusColors.missing}`}>
                          {cap.name}
                        </span>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Expanded */}
              {designDocExpanded && hasContent && (
                <div className="bg-neutral-900/60 border border-neutral-800 rounded-lg p-3 space-y-3 text-xs">
                  {docSummary && <p className="text-neutral-300">{docSummary}</p>}

                  {docArch && typeof docArch.type === 'string' && (
                    <div>
                      <span className="text-[9px] font-semibold text-neutral-500 uppercase tracking-wider">Architecture</span>
                      <p className="text-neutral-400 mt-0.5">{String(docArch.type)}{typeof docArch.description === 'string' ? ` — ${docArch.description.slice(0, 200)}` : ''}</p>
                    </div>
                  )}

                  {systemPages.length > 0 && (
                    <div>
                      <span className="text-[9px] font-semibold text-neutral-500 uppercase tracking-wider">Systems</span>
                      <div className="mt-1 space-y-1">
                        {systemPages.map(sys => (
                          <div key={sys.key} className="flex items-start gap-2">
                            <span className={`text-[9px] font-mono ${sysStatusColors[String(sys.status || 'missing')] || 'text-neutral-500'}`}>
                              {String(sys.status || '?').toUpperCase()}
                            </span>
                            <div className="flex-1 min-w-0">
                              <span className="text-neutral-200 font-medium">{String(sys.name || sys.id || sys.key)}</span>
                              {typeof sys.description === 'string' && <span className="text-neutral-500 ml-1.5">{sys.description.slice(0, 120)}</span>}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {capPages.length > 0 && (
                    <div>
                      <span className="text-[9px] font-semibold text-neutral-500 uppercase tracking-wider">Capabilities</span>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        {capPages.map(cap => (
                            <span key={cap.key} className={`text-[9px] px-1.5 py-0.5 rounded border ${capStatusColors[cap.status] || capStatusColors.missing}`} title={cap.notes}>
                              {cap.name}: {cap.status}
                            </span>
                          ))}
                      </div>
                    </div>
                  )}

                  {docRisks.length > 0 && (
                    <div>
                      <span className="text-[9px] font-semibold text-red-500/70 uppercase tracking-wider">Risks</span>
                      <ul className="mt-1 space-y-0.5">
                        {docRisks.map((risk, i) => (
                          <li key={i} className="text-neutral-400 flex items-start gap-1.5">
                            <span className="text-red-500/50 mt-0.5">-</span>
                            <span>{String(risk).slice(0, 200)}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {otherPages.length > 0 && (
                    <div>
                      <span className="text-[9px] font-semibold text-neutral-500 uppercase tracking-wider">Other Pages</span>
                      <div className="mt-1 space-y-1">
                        {otherPages.map(([key, val]) => (
                          <div key={key} className="text-neutral-400">
                            <span className="text-neutral-300 font-medium">{key}</span>
                            <span className="text-neutral-600 ml-1.5">{JSON.stringify(val).slice(0, 100)}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="text-[9px] text-neutral-600 pt-1 border-t border-neutral-800/50">
                    rev {String(meta.revision || 0)} — {pages.length} pages
                  </div>
                </div>
              )}

              {designDocExpanded && !hasContent && (
                <div className="bg-neutral-900/60 border border-neutral-800 border-dashed rounded-lg p-4 text-center">
                  <p className="text-xs text-neutral-500">No design document yet. The architecture agent will build one on the next strategic cycle.</p>
                </div>
              )}
            </div>
            );
          })()}

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
                          <div key={d.id} className={`border rounded-lg p-3 ${d.status === 'proposed' ? 'bg-purple-950/20 border-purple-900/40' : d.status === 'expired' ? 'bg-neutral-900/30 border-neutral-800/40 opacity-70' : 'bg-neutral-900/50 border-neutral-800/50'}`}>
                            {d.status === 'proposed' ? (
                              /* Proposed direction — approve or reject */
                              <div>
                                <div className="flex items-start gap-2">
                                  <p className="text-sm text-neutral-200 flex-1">{d.text}</p>
                                  <div className="flex items-center gap-1 shrink-0">
                                    <button
                                      onClick={() => approveProposal(d.id)}
                                      title="Approve — activate this direction"
                                      className="text-[10px] px-2 py-0.5 rounded border font-medium transition-colors bg-emerald-950/60 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/50"
                                    >
                                      Approve
                                    </button>
                                    <button
                                      onClick={() => rejectProposal(d.id)}
                                      title="Reject — remove this proposal"
                                      className="text-[10px] px-2 py-0.5 rounded border font-medium transition-colors bg-red-950/60 border-red-900/50 text-red-400 hover:bg-red-900/50"
                                    >
                                      Reject
                                    </button>
                                  </div>
                                </div>
                                {d.proposal_rationale && (
                                  <blockquote className="mt-2 border-l-2 border-purple-800/50 pl-2 text-[11px] text-purple-300/80 italic leading-relaxed">
                                    {d.proposal_rationale}
                                  </blockquote>
                                )}
                                <div className="flex items-center gap-2 mt-2">
                                  <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
                                    {d.priority}
                                  </span>
                                  <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${statusColors.proposed}`}>
                                    Proposed by agent
                                  </span>
                                  {d.proposed_at && (
                                    <span className="text-[10px] text-neutral-600 ml-auto">
                                      proposed {formatRelativeTime(d.proposed_at)}
                                    </span>
                                  )}
                                </div>
                              </div>
                            ) : d.status === 'expired' ? (
                              /* Expired direction — revive or approve */
                              <div>
                                <div className="flex items-start gap-2">
                                  <p className="text-sm text-neutral-400 flex-1">{d.text}</p>
                                  <div className="flex items-center gap-1 shrink-0">
                                    <button
                                      onClick={() => reviveProposal(d.id)}
                                      title="Revive — move back to proposed"
                                      className="text-[10px] px-2 py-0.5 rounded border font-medium transition-colors bg-purple-950/60 border-purple-900/50 text-purple-400 hover:bg-purple-900/50"
                                    >
                                      Revive
                                    </button>
                                    <button
                                      onClick={() => approveProposal(d.id)}
                                      title="Approve — activate this direction"
                                      className="text-[10px] px-2 py-0.5 rounded border font-medium transition-colors bg-emerald-950/60 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/50"
                                    >
                                      Approve
                                    </button>
                                  </div>
                                </div>
                                {d.proposal_rationale && (
                                  <blockquote className="mt-2 border-l-2 border-neutral-700/50 pl-2 text-[11px] text-neutral-500 italic leading-relaxed">
                                    {d.proposal_rationale}
                                  </blockquote>
                                )}
                                <div className="flex items-center gap-2 mt-2">
                                  <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
                                    {d.priority}
                                  </span>
                                  <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${statusColors.expired}`}>
                                    Expired
                                  </span>
                                  {d.proposed_at && (
                                    <span className="text-[10px] text-neutral-600 ml-auto">
                                      proposed {formatRelativeTime(d.proposed_at)}
                                    </span>
                                  )}
                                </div>
                              </div>
                            ) : editingId === d.id ? (
                              /* Edit mode */
                              <div className="space-y-2">
                                <input
                                  value={editText}
                                  onChange={e => setEditText(e.target.value)}
                                  className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                                />
                                <div className="bg-neutral-950 border border-neutral-700 rounded px-2 py-1.5 flex flex-wrap gap-1 items-center focus-within:ring-1 focus-within:ring-neutral-600">
                                  {editPathTags.map(tag => (
                                    <span key={tag} className="bg-neutral-800 border border-neutral-700 text-neutral-300 text-[10px] font-mono px-1.5 py-0.5 rounded flex items-center gap-1">
                                      {tag}
                                      <button
                                        type="button"
                                        onClick={() => setEditPathTags(prev => prev.filter(t => t !== tag))}
                                        className="text-neutral-500 hover:text-neutral-300 transition-colors leading-none"
                                      >
                                        <X className="w-2.5 h-2.5" />
                                      </button>
                                    </span>
                                  ))}
                                  <input
                                    value={editPathInput}
                                    onChange={e => setEditPathInput(e.target.value)}
                                    onKeyDown={e => {
                                      if (e.key === 'Enter' || e.key === ',') {
                                        e.preventDefault();
                                        const val = editPathInput.trim();
                                        if (val && !editPathTags.includes(val)) setEditPathTags(prev => [...prev, val]);
                                        setEditPathInput('');
                                      } else if (e.key === 'Backspace' && !editPathInput && editPathTags.length > 0) {
                                        setEditPathTags(prev => prev.slice(0, -1));
                                      }
                                    }}
                                    placeholder={editPathTags.length === 0 ? 'Path scope (e.g. backend/)' : ''}
                                    className="flex-1 min-w-[120px] bg-transparent text-neutral-100 text-xs focus:outline-none placeholder-neutral-600"
                                  />
                                </div>
                                <div className="flex items-center gap-2">
                                  <select
                                    value={editPriority}
                                    onChange={e => setEditPriority(e.target.value as 'high' | 'medium' | 'low')}
                                    className="bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
                                  >
                                    <option value="high">High</option>
                                    <option value="medium">Medium</option>
                                    <option value="low">Low</option>
                                  </select>
                                  <select
                                    value={editStatus}
                                    onChange={e => setEditStatus(e.target.value as 'active' | 'completed' | 'paused')}
                                    className="bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
                                  >
                                    <option value="active">Active</option>
                                    <option value="completed">Completed</option>
                                    <option value="paused">Paused</option>
                                  </select>
                                  <div className="flex-1" />
                                  <button onClick={() => updateDirection(d.id)} className="text-emerald-400 hover:text-emerald-300 transition-colors">
                                    <Check className="w-3.5 h-3.5" />
                                  </button>
                                  <button onClick={() => setEditingId(null)} className="text-neutral-500 hover:text-neutral-300 transition-colors">
                                    <X className="w-3.5 h-3.5" />
                                  </button>
                                </div>
                              </div>
                            ) : (
                              /* View mode */
                              <div>
                                <div className="flex items-start gap-2">
                                  <p className="text-sm text-neutral-200 flex-1">{d.text}</p>
                                  <div className="flex items-center gap-1 shrink-0">
                                    {key === 'active' && (() => {
                                      const activeItems = (grouped.find(g => g.key === 'active') ?? { items: [] }).items;
                                      const idx = activeItems.findIndex(x => x.id === d.id);
                                      return (<>
                                        {idx > 0 && (
                                          <button
                                            onClick={() => {
                                              const next = [...activeItems];
                                              [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
                                              reorderDirections(next);
                                            }}
                                            className="text-neutral-400 hover:text-neutral-300 transition-colors p-0.5"
                                            title="Move up"
                                          >
                                            <ChevronUp className="w-3 h-3" />
                                          </button>
                                        )}
                                        {idx < activeItems.length - 1 && (
                                          <button
                                            onClick={() => {
                                              const next = [...activeItems];
                                              [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
                                              reorderDirections(next);
                                            }}
                                            className="text-neutral-400 hover:text-neutral-300 transition-colors p-0.5"
                                            title="Move down"
                                          >
                                            <ChevronDown className="w-3 h-3" />
                                          </button>
                                        )}
                                      </>);
                                    })()}
                                    {key === 'active' && (() => {
                                      const cooldownStatus = getCooldownStatus(d, cooldownMinutes);
                                      const inCooldown = cooldownStatus === 'cooling';
                                      return (
                                        <div className="relative flex items-center gap-1">
                                          <button
                                            onClick={() => !inCooldown && triggerResearch(d.id)}
                                            disabled={researchingIds[d.id] || inCooldown}
                                            title={inCooldown ? 'Direction is in cooldown — use force to bypass' : 'Trigger research for this direction'}
                                            className="text-[10px] px-1.5 py-0.5 rounded border font-medium transition-colors disabled:opacity-40 bg-neutral-800/60 border-neutral-700/60 text-neutral-400 hover:enabled:text-neutral-200 hover:enabled:bg-neutral-700/60"
                                          >
                                            {researchingIds[d.id] ? '…' : researchSuccessIds[d.id] ? 'Queued' : 'Research'}
                                          </button>
                                          {inCooldown && (
                                            <div className="relative">
                                              <button
                                                onClick={() => setForceConfirmId(forceConfirmId === d.id ? null : d.id)}
                                                disabled={researchingIds[d.id]}
                                                title="Force research (bypass cooldown)"
                                                className="text-[10px] px-1 py-0.5 rounded border font-medium transition-colors disabled:opacity-40 bg-amber-950/40 border-amber-900/50 text-amber-400 hover:bg-amber-900/40"
                                              >
                                                <Zap className="w-2.5 h-2.5" />
                                              </button>
                                              {forceConfirmId === d.id && (
                                                <div
                                                  ref={forceConfirmRef}
                                                  className="absolute right-0 top-6 z-10 w-52 bg-neutral-900 border border-amber-900/60 rounded-lg p-3 shadow-xl"
                                                >
                                                  <p className="text-[11px] text-amber-300 font-medium mb-1">Force research?</p>
                                                  <p className="text-[10px] text-neutral-400 mb-2 leading-relaxed">Bypasses the cooldown and queues a research task immediately.</p>
                                                  <div className="flex gap-2">
                                                    <button
                                                      onClick={() => triggerResearch(d.id, true)}
                                                      className="flex-1 text-[10px] px-2 py-1 rounded border bg-amber-950/60 border-amber-900/50 text-amber-300 hover:bg-amber-900/50 font-medium transition-colors"
                                                    >
                                                      Force
                                                    </button>
                                                    <button
                                                      onClick={() => setForceConfirmId(null)}
                                                      className="flex-1 text-[10px] px-2 py-1 rounded border bg-neutral-800 border-neutral-700 text-neutral-400 hover:text-neutral-200 font-medium transition-colors"
                                                    >
                                                      Cancel
                                                    </button>
                                                  </div>
                                                </div>
                                              )}
                                            </div>
                                          )}
                                        </div>
                                      );
                                    })()}
                                    <button onClick={() => startEdit(d)} className="text-neutral-400 hover:text-neutral-300 transition-colors p-0.5">
                                      <Pencil className="w-3 h-3" />
                                    </button>
                                    <button onClick={() => deleteDirection(d.id)} className="text-neutral-400 hover:text-red-400 transition-colors p-0.5">
                                      <Trash2 className="w-3 h-3" />
                                    </button>
                                  </div>
                                </div>
                                {/* Last researched / cooldown subtitle */}
                                {key === 'active' && (() => {
                                  const cooldownStatus = getCooldownStatus(d, cooldownMinutes);
                                  if (cooldownStatus === 'never') {
                                    return <p className="text-[10px] text-neutral-600 mt-0.5">Never researched</p>;
                                  }
                                  if (cooldownStatus === 'cooling') {
                                    return <p className="text-[10px] text-amber-500/80 mt-0.5">Last researched {formatRelativeTime(d.last_researched_at ?? '')}</p>;
                                  }
                                  return <p className="text-[10px] text-neutral-600 mt-0.5">Last researched {formatRelativeTime(d.last_researched_at ?? '')}</p>;
                                })()}
                                {/* Auto-pause reason subtitle */}
                                {key === 'paused' && d.paused_reason && (
                                  <p className="text-[10px] text-amber-500/80 mt-0.5 leading-relaxed">
                                    {d.paused_reason}
                                    {d.paused_at && <span className="text-neutral-600"> · paused {formatRelativeTime(d.paused_at)}</span>}
                                  </p>
                                )}
                                {/* Positive completion suggestion */}
                                {d.possibly_complete && d.completion_suggestion && (
                                  <p className="text-[10px] text-emerald-500/80 mt-0.5 leading-relaxed">
                                    {d.completion_suggestion}
                                  </p>
                                )}
                                <div className="flex items-center gap-2 mt-2">
                                  <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
                                    {d.priority}
                                  </span>
                                  {d.status === 'active' || d.status === 'paused' ? (
                                    <button
                                      onClick={() => quickToggleStatus(d.id, d.status as 'active' | 'paused')}
                                      title={d.status === 'active' ? 'Click to pause' : 'Click to activate'}
                                      className={`text-[10px] px-1.5 py-0.5 rounded border font-medium transition-opacity hover:opacity-70 cursor-pointer ${statusColors[d.status]}`}
                                    >
                                      {d.status}
                                    </button>
                                  ) : (
                                    <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${statusColors[d.status]}`}>
                                      {d.status}
                                    </span>
                                  )}
                                  {/* Reactivate button for auto-paused directions */}
                                  {d.auto_paused && key === 'paused' && (
                                    <button
                                      onClick={() => reactivateDirection(d.id)}
                                      className="text-[10px] px-1.5 py-0.5 rounded border font-medium transition-colors bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40"
                                    >
                                      Reactivate
                                    </button>
                                  )}
                                  {/* Mark Complete button for possibly-complete directions */}
                                  {d.possibly_complete && key === 'active' && (
                                    <button
                                      onClick={() => markDirectionComplete(d.id)}
                                      className="text-[10px] px-1.5 py-0.5 rounded border font-medium transition-colors bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40"
                                    >
                                      Mark Complete
                                    </button>
                                  )}
                                  {/* Cooldown badge */}
                                  {key === 'active' && (() => {
                                    const cooldownStatus = getCooldownStatus(d, cooldownMinutes);
                                    if (cooldownStatus === 'cooling') {
                                      return (
                                        <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 text-amber-400 border-amber-900/50">
                                          Cooling down · {formatTimeRemaining(d.last_researched_at ?? '', cooldownMinutes)}
                                        </span>
                                      );
                                    }
                                    return (
                                      <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 text-emerald-400 border-emerald-900/50">
                                        Ready
                                      </span>
                                    );
                                  })()}
                                  <span className="text-[10px] text-neutral-600 ml-auto">
                                    {new Date(d.added_at).toLocaleDateString()}
                                  </span>
                                  {(taskCounts[d.id] ?? 0) > 0 && (
                                    <span className="text-[10px] text-neutral-500">
                                      {taskCounts[d.id]} {taskCounts[d.id] === 1 ? 'task' : 'tasks'}
                                    </span>
                                  )}
                                </div>
                                {d.paths && d.paths.length > 0 && (
                                  <div className="flex flex-wrap gap-1 mt-1.5">
                                    {d.paths.map(p => (
                                      <span key={p} className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800/60 border border-neutral-700/60 text-neutral-400 font-mono">
                                        {p}
                                      </span>
                                    ))}
                                  </div>
                                )}
                                {d.notes && d.notes.length > 0 && (
                                  <div className="mt-2 p-1.5 bg-neutral-800/30 border border-neutral-700/40 rounded text-xs">
                                    <div className="text-neutral-500 font-medium mb-1">Agent notes ({d.notes.length}):</div>
                                    {d.notes.slice(-3).map((n, ni) => (
                                      <div key={ni} className="text-neutral-500 leading-relaxed">• {n.text.slice(0, 120)}</div>
                                    ))}
                                  </div>
                                )}
                                {d.pending_update && (
                                  <div className="mt-2 p-2 bg-amber-950/20 border border-amber-900/30 rounded text-xs">
                                    <div className="text-amber-400 font-medium mb-1">Agent suggests update:</div>
                                    {d.pending_update.suggested_text && d.pending_update.suggested_text !== d.text && (
                                      <div className="text-neutral-300">Text: &ldquo;{d.pending_update.suggested_text.slice(0, 150)}&rdquo;</div>
                                    )}
                                    {d.pending_update.suggested_priority && d.pending_update.suggested_priority !== d.priority && (
                                      <div className="text-neutral-400">Priority: {d.priority} → {d.pending_update.suggested_priority}</div>
                                    )}
                                    {d.pending_update.rationale && (
                                      <div className="text-neutral-500 italic mt-1">{d.pending_update.rationale}</div>
                                    )}
                                    <div className="flex gap-2 mt-2">
                                      <button onClick={() => applyDirectionUpdate(d)} className="text-xs px-2 py-0.5 rounded border bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40 transition-colors">Apply</button>
                                      <button onClick={() => dismissDirectionUpdate(d.id)} className="text-xs px-2 py-0.5 rounded border bg-neutral-800/60 border-neutral-700/60 text-neutral-400 hover:text-neutral-300 transition-colors">Dismiss</button>
                                    </div>
                                  </div>
                                )}
                                {d.research_history && d.research_history.length > 0 && (
                                  <div className="mt-2">
                                    <button
                                      onClick={() => setHistoryOpenIds(prev => ({ ...prev, [d.id]: !prev[d.id] }))}
                                      className="flex items-center gap-1.5 w-full text-left group"
                                    >
                                      {historyOpenIds[d.id]
                                        ? <ChevronDown className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                                        : <ChevronRight className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                                      }
                                      <span className="text-[10px] font-medium text-neutral-500 uppercase tracking-wider group-hover:text-neutral-400 transition-colors flex-1">
                                        Research History ({d.research_history.length})
                                      </span>
                                      {d.possibly_saturated && (
                                        <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 text-amber-400 border-amber-900/50">
                                          possibly saturated
                                        </span>
                                      )}
                                      {d.possibly_complete && (
                                        <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 text-emerald-400 border-emerald-900/50">
                                          possibly complete
                                        </span>
                                      )}
                                    </button>
                                    {historyOpenIds[d.id] && (
                                      <div className="mt-1.5 space-y-2">
                                        {[...d.research_history].reverse().slice(0, 5).map((entry, i) => (
                                          <div key={entry.task_id ?? i} className="p-2 bg-neutral-900/60 border border-neutral-800/60 rounded text-[10px]">
                                            <div className="flex items-center justify-between mb-1">
                                              <span className="text-neutral-400 font-medium">{formatRelativeTime(entry.timestamp)}</span>
                                              <div className="flex items-center gap-1.5 text-neutral-600">
                                                <span>{entry.tasks_created} created</span>
                                                {entry.tasks_completed != null && (
                                                  <span className="text-emerald-600">{entry.tasks_completed} done</span>
                                                )}
                                                {entry.tasks_failed != null && entry.tasks_failed > 0 && (
                                                  <span className="text-red-600">{entry.tasks_failed} failed</span>
                                                )}
                                              </div>
                                            </div>
                                            {entry.finding_titles.length > 0 && (
                                              <p className="text-neutral-500 leading-relaxed">
                                                {entry.finding_titles.join(', ')}
                                              </p>
                                            )}
                                            {entry.key_finding_descriptions.length > 0 && (
                                              <div className="mt-1 space-y-0.5">
                                                {entry.key_finding_descriptions.map((desc, j) => (
                                                  <p key={j} className="text-neutral-600 leading-relaxed">
                                                    {desc.length > 100 ? desc.slice(0, 100) + '…' : desc}
                                                  </p>
                                                ))}
                                              </div>
                                            )}
                                            {entry.cycle_insights && entry.cycle_insights.length > 0 && (
                                              <div className="mt-1 space-y-0.5 border-t border-neutral-800/60 pt-1">
                                                {entry.cycle_insights.map((insight, j) => (
                                                  <p key={j} className="text-blue-500/70 leading-relaxed italic">
                                                    {insight}
                                                  </p>
                                                ))}
                                              </div>
                                            )}
                                          </div>
                                        ))}
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
