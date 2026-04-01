import { useState, useEffect } from 'react';
import { Plus, Pencil, Trash2, Check, X, ToggleLeft, ToggleRight } from 'lucide-react';
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

export function DirectionsPanel({ onClose }: { onClose: () => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [directions, setDirections] = useState<Direction[]>([]);
  const [autonomousEnabled, setAutonomousEnabled] = useState(false);
  const [loading, setLoading] = useState(false);

  // Add form
  const [newText, setNewText] = useState('');
  const [newPriority, setNewPriority] = useState<'high' | 'medium' | 'low'>('medium');

  // Error feedback
  const [error, setError] = useState<string | null>(null);

  // Inline edit
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');
  const [editPriority, setEditPriority] = useState<'high' | 'medium' | 'low'>('medium');
  const [editStatus, setEditStatus] = useState<'active' | 'completed' | 'paused'>('active');

  useEffect(() => {
    fetchProjects();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (selectedProjectId) fetchDirections();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  async function fetchProjects() {
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects`, { headers: h });
      if (!res.ok) throw new Error('Failed to fetch projects');
      const data = await res.json();
      const list = Array.isArray(data) ? data : data.projects ?? [];
      setProjects(list);
      if (list.length > 0 && !selectedProjectId) setSelectedProjectId(list[0].id);
    } catch { /* ignore */ }
  }

  async function fetchDirections() {
    setLoading(true);
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions`, { headers: h });
      if (!res.ok) throw new Error('Failed to fetch directions');
      const data = await res.json();
      setDirections(data.directions ?? []);
      setAutonomousEnabled(data.autonomous_enabled ?? false);
    } catch { /* ignore */ }
    setLoading(false);
  }

  async function addDirection() {
    if (!newText.trim() || !selectedProjectId) return;
    setError(null);
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/directions`, {
        method: 'POST', headers: h,
        body: JSON.stringify({ text: newText.trim(), priority: newPriority }),
      });
      if (!res.ok) throw new Error('Failed to add direction');
      setNewText('');
      setNewPriority('medium');
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
        body: JSON.stringify({ text: editText, priority: editPriority, status: editStatus }),
      });
      if (!res.ok) throw new Error('Failed to update direction');
      setEditingId(null);
      fetchDirections();
    } catch {
      setError('Failed to update direction. Please try again.');
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

  function startEdit(d: Direction) {
    setEditingId(d.id);
    setEditText(d.text);
    setEditPriority(d.priority);
    setEditStatus(d.status);
  }

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
            <div className="space-y-2">
              {directions.map(d => (
                <div key={d.id} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3">
                  {editingId === d.id ? (
                    /* Edit mode */
                    <div className="space-y-2">
                      <input
                        value={editText}
                        onChange={e => setEditText(e.target.value)}
                        className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                      />
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
                          <button onClick={() => startEdit(d)} className="text-neutral-600 hover:text-neutral-300 transition-colors p-0.5">
                            <Pencil className="w-3 h-3" />
                          </button>
                          <button onClick={() => deleteDirection(d.id)} className="text-neutral-600 hover:text-red-400 transition-colors p-0.5">
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </div>
                      </div>
                      <div className="flex items-center gap-2 mt-2">
                        <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
                          {d.priority}
                        </span>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${statusColors[d.status]}`}>
                          {d.status}
                        </span>
                        <span className="text-[10px] text-neutral-600 ml-auto">
                          {new Date(d.added_at).toLocaleDateString()}
                        </span>
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
