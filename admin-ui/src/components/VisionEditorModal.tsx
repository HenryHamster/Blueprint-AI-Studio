import { useState } from 'react';
import { X, Plus, Trash2 } from 'lucide-react';
import { API_BASE_URL, authHeaders } from '../api';

interface SystemEntry {
  id: string;
  name: string;
  description: string;
  status: string;
  depends_on: string[];
}

interface VisionEditorModalProps {
  projectId: string;
  initialVision: Record<string, unknown>;
  onSave: (vision: Record<string, unknown>) => void;
  onClose: () => void;
}

const STATUS_OPTIONS = ['planned', 'in_progress', 'functional', 'mature'] as const;

export function VisionEditorModal({ projectId, initialVision, onSave, onClose }: VisionEditorModalProps) {
  const [purpose, setPurpose] = useState(typeof initialVision.purpose === 'string' ? initialVision.purpose : '');
  const [criteria, setCriteria] = useState<string[]>(
    Array.isArray(initialVision.success_criteria) ? (initialVision.success_criteria as string[]).map(String) : []
  );
  const [newCriterion, setNewCriterion] = useState('');
  const [systems, setSystems] = useState<SystemEntry[]>(() => {
    const raw = Array.isArray(initialVision.systems) ? initialVision.systems as Array<Record<string, unknown>> : [];
    return raw.map(s => ({
      id: String(s.id || `sys-${Math.random().toString(36).slice(2, 8)}`),
      name: String(s.name || ''),
      description: String(s.description || ''),
      status: String(s.status || 'planned'),
      depends_on: Array.isArray(s.depends_on) ? (s.depends_on as string[]).map(String) : [],
    }));
  });
  const [context, setContext] = useState(typeof initialVision.context === 'string' ? initialVision.context : '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function addCriterion() {
    const trimmed = newCriterion.trim();
    if (!trimmed) return;
    setCriteria(prev => [...prev, trimmed]);
    setNewCriterion('');
  }

  function removeCriterion(index: number) {
    setCriteria(prev => prev.filter((_, i) => i !== index));
  }

  function addSystem() {
    setSystems(prev => [...prev, {
      id: `sys-${Math.random().toString(36).slice(2, 8)}`,
      name: '',
      description: '',
      status: 'planned',
      depends_on: [],
    }]);
  }

  function removeSystem(index: number) {
    setSystems(prev => prev.filter((_, i) => i !== index));
  }

  function updateSystem(index: number, field: keyof SystemEntry, value: string | string[]) {
    setSystems(prev => prev.map((s, i) => i === index ? { ...s, [field]: value } : s));
  }

  async function handleSave() {
    if (!purpose.trim()) {
      setError('Purpose is required — it tells the system what to build.');
      return;
    }
    setSaving(true);
    setError(null);
    // Strip internal fields before spreading
    const { _strategic_failures: _, ...cleanVision } = initialVision as Record<string, unknown> & { _strategic_failures?: unknown };
    const visionPayload: Record<string, unknown> = {
      ...cleanVision,
      purpose: purpose.trim(),
      success_criteria: criteria,
      systems: systems.filter(s => s.name.trim()).map(s => ({
        id: s.id,
        name: s.name.trim(),
        description: s.description.trim(),
        status: s.status,
        depends_on: s.depends_on,
      })),
      context,
    };
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${projectId}`, {
        method: 'PATCH',
        headers: h,
        body: JSON.stringify({ vision: visionPayload }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onSave(visionPayload);
    } catch {
      setError('Failed to save vision. Please try again.');
    }
    setSaving(false);
  }

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 z-50" onClick={onClose}>
      <div
        className="bg-neutral-900 rounded-xl shadow-2xl w-full max-w-lg mx-3 overflow-y-auto max-h-[90vh] border border-neutral-800"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-neutral-800">
          <h2 className="text-sm font-semibold text-neutral-100">Edit Product Vision</h2>
          <button onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-5 py-4 space-y-4">
          {/* Purpose */}
          <div>
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Purpose</label>
            <textarea
              value={purpose}
              onChange={e => setPurpose(e.target.value)}
              rows={3}
              placeholder="What this product is and who it's for"
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-cyan-600 resize-y"
            />
          </div>

          {/* Success Criteria */}
          <div>
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Success Criteria</label>
            <div className="space-y-1">
              {criteria.map((c, i) => (
                <div key={i} className="flex items-center gap-2 group">
                  <span className="text-xs text-neutral-300 flex-1">- {c}</span>
                  <button
                    onClick={() => removeCriterion(i)}
                    className="text-neutral-700 hover:text-red-400 transition-colors opacity-0 group-hover:opacity-100"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
            <div className="flex items-center gap-2 mt-1.5">
              <input
                value={newCriterion}
                onChange={e => setNewCriterion(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addCriterion(); } }}
                placeholder="Add criterion..."
                className="flex-1 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2.5 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-cyan-600"
              />
              <button
                onClick={addCriterion}
                className="text-neutral-500 hover:text-cyan-400 transition-colors"
              >
                <Plus className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          {/* Systems */}
          <div>
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Systems</label>
            <div className="space-y-2">
              {systems.map((sys, i) => (
                <div key={sys.id} className="bg-neutral-950 border border-neutral-800 rounded-lg p-2.5 space-y-1.5 group">
                  <div className="flex items-center gap-2">
                    <input
                      value={sys.name}
                      onChange={e => updateSystem(i, 'name', e.target.value)}
                      placeholder="System name"
                      className="flex-1 bg-transparent border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-cyan-600"
                    />
                    <select
                      value={sys.status}
                      onChange={e => updateSystem(i, 'status', e.target.value)}
                      className="bg-neutral-900 border border-neutral-700 text-neutral-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-cyan-600"
                    >
                      {STATUS_OPTIONS.map(s => (
                        <option key={s} value={s}>{s.replace('_', ' ')}</option>
                      ))}
                    </select>
                    <button
                      onClick={() => removeSystem(i)}
                      className="text-neutral-700 hover:text-red-400 transition-colors opacity-0 group-hover:opacity-100"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                  <input
                    value={sys.description}
                    onChange={e => updateSystem(i, 'description', e.target.value)}
                    placeholder="Description"
                    className="w-full bg-transparent border border-neutral-800 text-neutral-400 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-cyan-600"
                  />
                </div>
              ))}
            </div>
            <button
              onClick={addSystem}
              className="flex items-center gap-1 text-[11px] text-neutral-500 hover:text-cyan-400 transition-colors mt-1.5"
            >
              <Plus className="w-3 h-3" /> Add system
            </button>
          </div>

          {/* Context */}
          <div>
            <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-1.5">Context</label>
            <textarea
              value={context}
              onChange={e => setContext(e.target.value)}
              rows={4}
              placeholder="Strategic reasoning, open questions, confidence gaps..."
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-cyan-600 resize-y"
            />
          </div>

          {error && (
            <p className="text-xs text-red-400">{error}</p>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-neutral-800">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-xs text-neutral-400 hover:text-neutral-200 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-1.5 text-xs font-medium text-neutral-100 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 rounded-lg transition-colors"
          >
            {saving ? 'Saving...' : 'Save Vision'}
          </button>
        </div>
      </div>
    </div>
  );
}
