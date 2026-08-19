import { useState } from 'react';
import { X, Plus, Trash2 } from 'lucide-react';
import { apiFetch } from '../api';
import type { ProblemModel, ProblemModelPatch } from '../types';

interface ProblemModelEditorProps {
  projectId: string;
  problemModel: ProblemModel;
  onClose: () => void;
  onSaved: (patched: ProblemModel) => void;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
}

// Modal that lets the user edit the subset of problem_model fields they
// own: intent (purpose + current_focus), context, constraints. Gaps,
// capabilities, friction are agent-authored and not editable here — the
// backend rejects those keys on the PATCH payload.
export function ProblemModelEditor({
  projectId,
  problemModel,
  onClose,
  onSaved,
  addToast,
}: ProblemModelEditorProps) {
  const [purpose, setPurpose] = useState(problemModel.intent?.purpose ?? '');
  const [currentFocus, setCurrentFocus] = useState(problemModel.intent?.current_focus ?? '');
  const [projectType, setProjectType] = useState(problemModel.context?.project_type ?? '');
  const [users, setUsers] = useState(problemModel.context?.users ?? '');
  const [team, setTeam] = useState(problemModel.context?.team ?? '');
  const [deployCadence, setDeployCadence] = useState(problemModel.context?.deploy_cadence ?? '');
  const [notes, setNotes] = useState(problemModel.context?.notes ?? '');
  const [constraints, setConstraints] = useState<string[]>(problemModel.constraints ?? []);
  const [saving, setSaving] = useState(false);

  function addConstraint() {
    setConstraints((prev) => [...prev, '']);
  }

  function updateConstraint(idx: number, value: string) {
    setConstraints((prev) => prev.map((c, i) => (i === idx ? value : c)));
  }

  function removeConstraint(idx: number) {
    setConstraints((prev) => prev.filter((_, i) => i !== idx));
  }

  async function handleSave() {
    setSaving(true);
    try {
      const trimmedConstraints = constraints.map((c) => c.trim()).filter((c) => c.length > 0);
      const patch: ProblemModelPatch = {
        intent: {
          purpose: purpose.trim(),
          current_focus: currentFocus.trim(),
        },
        context: {
          project_type: projectType.trim() || null,
          users: users.trim() || null,
          team: team.trim() || null,
          deploy_cadence: deployCadence.trim() || null,
          notes: notes.trim() || null,
        },
        constraints: trimmedConstraints,
      };
      const res = await apiFetch(`/api/admin/projects/${projectId}`, {
        method: 'PATCH',
        body: JSON.stringify({ problem_model_patch: patch }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const msg = body?.detail || body?.error?.message || `HTTP ${res.status}`;
        throw new Error(msg);
      }
      const updated = await res.json();
      addToast('Problem model updated', 'success');
      onSaved(updated.problem_model ?? {});
    } catch (err) {
      console.error('Failed to save problem_model:', err);
      const msg = err instanceof Error ? err.message : 'Save failed';
      addToast(msg, 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
    >
      <div className="bg-workbench-shell border border-neutral-800 rounded-lg w-full max-w-2xl max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-3 border-b border-neutral-800">
          <h3 className="text-sm font-semibold text-neutral-100">Edit intent & constraints</h3>
          <button
            type="button"
            onClick={onClose}
            className="text-neutral-500 hover:text-neutral-200 transition"
            aria-label="Close"
            data-viewport-allow-small-target
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto custom-scrollbar px-5 py-4 space-y-5">
          {/* Intent */}
          <div>
            <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">Intent</h4>
            <label className="block mb-3">
              <span className="block text-xs text-neutral-500 mb-1">Purpose — long-term, what this project is for</span>
              <textarea
                value={purpose}
                onChange={(e) => setPurpose(e.target.value)}
                rows={2}
                placeholder="Build an autonomous coding studio — agents ship merged PRs without intervention."
                className="w-full px-3 py-2 text-sm bg-workbench-base border border-neutral-800 rounded text-neutral-100 placeholder-neutral-600 outline-none workbench-focus"
              />
            </label>
            <label className="block">
              <span className="block text-xs text-neutral-500 mb-1">Current focus — what you're actively working toward right now</span>
              <textarea
                value={currentFocus}
                onChange={(e) => setCurrentFocus(e.target.value)}
                rows={2}
                placeholder="Stabilize the tactical→review→merge loop at 70%+ first-attempt merge rate."
                className="w-full px-3 py-2 text-sm bg-workbench-base border border-neutral-800 rounded text-neutral-100 placeholder-neutral-600 outline-none workbench-focus"
              />
            </label>
          </div>

          {/* Context */}
          <div>
            <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">Context</h4>
            <p className="text-xs text-neutral-500 mb-2">
              How this project is set up — shapes how the agent weighs risk and cadence.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="block">
                <span className="block text-xs text-neutral-500 mb-1">Project type</span>
                <input
                  type="text"
                  value={projectType}
                  onChange={(e) => setProjectType(e.target.value)}
                  placeholder="startup / research / product / personal / enterprise"
                  className="w-full px-3 py-1.5 text-sm bg-workbench-base border border-neutral-800 rounded text-neutral-100 placeholder-neutral-600 outline-none workbench-focus"
                />
              </label>
              <label className="block">
                <span className="block text-xs text-neutral-500 mb-1">Team</span>
                <input
                  type="text"
                  value={team}
                  onChange={(e) => setTeam(e.target.value)}
                  placeholder="solo / 3 engineers + 1 PM / etc."
                  className="w-full px-3 py-1.5 text-sm bg-workbench-base border border-neutral-800 rounded text-neutral-100 placeholder-neutral-600 outline-none workbench-focus"
                />
              </label>
              <label className="block">
                <span className="block text-xs text-neutral-500 mb-1">Users</span>
                <input
                  type="text"
                  value={users}
                  onChange={(e) => setUsers(e.target.value)}
                  placeholder="Who this is for, in one line"
                  className="w-full px-3 py-1.5 text-sm bg-workbench-base border border-neutral-800 rounded text-neutral-100 placeholder-neutral-600 outline-none workbench-focus"
                />
              </label>
              <label className="block">
                <span className="block text-xs text-neutral-500 mb-1">Deploy cadence</span>
                <input
                  type="text"
                  value={deployCadence}
                  onChange={(e) => setDeployCadence(e.target.value)}
                  placeholder="continuous / weekly / quarterly / sporadic"
                  className="w-full px-3 py-1.5 text-sm bg-workbench-base border border-neutral-800 rounded text-neutral-100 placeholder-neutral-600 outline-none workbench-focus"
                />
              </label>
            </div>
            <label className="block mt-3">
              <span className="block text-xs text-neutral-500 mb-1">Notes — anything else that shapes decisions (risk tolerance, compliance, etc.)</span>
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                rows={3}
                className="w-full px-3 py-2 text-sm bg-workbench-base border border-neutral-800 rounded text-neutral-100 placeholder-neutral-600 outline-none workbench-focus"
              />
            </label>
          </div>

          {/* Constraints */}
          <div>
            <h4 className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-2">Constraints</h4>
            <p className="text-xs text-neutral-500 mb-2">
              Hard rules and preferences the agent must respect. One per line.
            </p>
            <div className="space-y-2">
              {constraints.map((c, i) => (
                <div key={i} className="flex items-start gap-2">
                  <input
                    type="text"
                    value={c}
                    onChange={(e) => updateConstraint(i, e.target.value)}
                    placeholder="e.g., Don't modify .github/workflows/"
                    className="flex-1 px-3 py-1.5 text-sm bg-workbench-base border border-neutral-800 rounded text-neutral-100 placeholder-neutral-600 outline-none workbench-focus"
                  />
                  <button
                    type="button"
                    onClick={() => removeConstraint(i)}
                    className="shrink-0 p-1.5 text-neutral-500 hover:text-rose-400 transition"
                    aria-label="Remove constraint"
                    data-viewport-allow-small-target
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={addConstraint}
                className="inline-flex items-center gap-1 px-2 py-1 text-xs text-neutral-400 hover:text-neutral-100 transition"
                data-viewport-allow-small-target
              >
                <Plus size={12} />
                Add constraint
              </button>
            </div>
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-neutral-800">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="min-h-[44px] px-3 py-1.5 text-xs font-medium rounded border border-neutral-800 text-neutral-400 hover:bg-workbench-hover/50 hover:text-neutral-200 transition"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
            className="min-h-[44px] px-3 py-1.5 text-xs font-medium rounded border border-cyan-500/40 bg-cyan-500/10 text-cyan-300 hover:bg-cyan-500/20 transition disabled:opacity-40"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
