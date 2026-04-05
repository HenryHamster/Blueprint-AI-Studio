import { useState, useEffect, useRef } from 'react';
import { Plus, Pencil, Trash2, Check, X, ToggleLeft, ToggleRight, ChevronDown, ChevronRight, ChevronUp, Settings, Zap } from 'lucide-react';
import { API_BASE_URL, authHeaders } from '../api';

interface Project {
  id: string;
  name?: string;
  [key: string]: unknown;
}

interface Direction {
  id: string;
  text: string;
  priority: 'high' | 'medium' | 'low';
  added_at: string;
  status: 'active' | 'completed' | 'paused';
  paths?: string[];
  last_researched_at?: string | null;
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
};

const priorityOrder: Record<string, number> = { high: 0, medium: 1, low: 2 };

const STATUS_GROUPS: Array<{ key: 'active' | 'paused' | 'completed'; label: string }> = [
  { key: 'active', label: 'Active' },
  { key: 'paused', label: 'Paused' },
  { key: 'completed', label: 'Completed' },
];

export function DirectionsPanel({ onClose, addToast }: { onClose: () => void; addToast: (message: string, type: 'success' | 'error' | 'info') => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [directions, setDirections] = useState<Direction[]>([]);
  const [autonomousEnabled, setAutonomousEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [taskCounts, setTaskCounts] = useState<Record<string, number>>({});

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
  const [editStatus, setEditStatus] = useState<'active' | 'completed' | 'paused'>('active');
  const [editPathTags, setEditPathTags] = useState<string[]>([]);
  const [editPathInput, setEditPathInput] = useState('');

  // Collapsible sections — all expanded by default
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

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
      // Full reorder: active in new order, then non-active in original order
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

  return (
    <div className="fixed inset-0 z-50 flex">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />

      {/* Panel */}
      <div className="relative ml-auto w-full max-w-lg h-full bg-neutral-950 border-l border-neutral-800 flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-neutral-800 shrink-0">
          <h2 className="text-sm font-semibold text-neutral-100 uppercase tracking-wider">Directions</h2>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors">
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
                className="bg-neutral-800 hover:bg-neutral-700 disabled:opacity-30 text-neutral-300 px-3 py-1.5 rounded text-xs transition-colors border border-neutral-700"
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
                          <div key={d.id} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3">
                            {editingId === d.id ? (
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
                                      const activeItems = grouped.find(g => g.key === 'active')!.items;
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
                                    return <p className="text-[10px] text-amber-500/80 mt-0.5">Last researched {formatRelativeTime(d.last_researched_at!)}</p>;
                                  }
                                  return <p className="text-[10px] text-neutral-600 mt-0.5">Last researched {formatRelativeTime(d.last_researched_at!)}</p>;
                                })()}
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
                                  {/* Cooldown badge */}
                                  {key === 'active' && (() => {
                                    const cooldownStatus = getCooldownStatus(d, cooldownMinutes);
                                    if (cooldownStatus === 'cooling') {
                                      return (
                                        <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 text-amber-400 border-amber-900/50">
                                          Cooling down · {formatTimeRemaining(d.last_researched_at!, cooldownMinutes)}
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
