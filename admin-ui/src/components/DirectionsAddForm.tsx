import { Plus, X } from 'lucide-react';
import type { useDirectionForm } from '../hooks/useDirectionForm';

export interface DirectionsAddFormProps {
  form: ReturnType<typeof useDirectionForm>;
  addDirection: () => void;
  // Open release version to stage against, or null when none is open. When null,
  // the "Stage for next version" control is disabled (the backend rejects an
  // atomic staged create with no open version), mirroring the edit-form option.
  stageTargetVersion?: number | null;
  // When set, show a badge indicating this direction is being created to address
  // a specific problem model gap (populated via "Spawn direction" on a gap card).
  linkedGapId?: string | null;
}

/**
 * Add-direction form: text input, priority/category selects, path-tag editor,
 * and completion-criteria textarea. Extracted verbatim from DirectionsPanel.
 */
export function DirectionsAddForm({ form, addDirection, stageTargetVersion, linkedGapId }: DirectionsAddFormProps) {
  const canStage = stageTargetVersion != null;
  const staged = canStage && form.newStaged;
  return (
    <div className="px-5 py-3 border-b border-neutral-800 shrink-0">
      {linkedGapId && (
        <div className="mb-1.5 flex items-center gap-1.5 text-xs">
          <span className="text-neutral-500">For gap:</span>
          <span className="px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-300 border border-cyan-500/20 font-mono">
            {linkedGapId}
          </span>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <input
          value={form.newText}
          onChange={e => form.setNewText(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && addDirection()}
          placeholder="New direction..."
          className="flex-1 min-w-0 basis-full sm:basis-auto bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm placeholder-neutral-600 workbench-focus"
        />
        <select
          value={form.newPriority}
          onChange={e => form.setNewPriority(e.target.value as 'high' | 'medium' | 'low')}
          className="min-w-0 shrink bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs workbench-focus"
        >
          <option value="high">High</option>
          <option value="medium">Medium</option>
          <option value="low">Low</option>
        </select>
        <select
          value={form.newCategory ?? ''}
          onChange={e => form.setNewCategory((e.target.value || null) as 'product' | 'structural' | 'mixed' | null)}
          className="min-w-0 shrink bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs workbench-focus"
        >
          <option value="">Type…</option>
          <option value="product">Product</option>
          <option value="structural">Structural</option>
          <option value="mixed">Mixed</option>
        </select>
        <button
          onClick={addDirection}
          disabled={!form.newText.trim()}
          className="shrink-0 bg-workbench-panel hover:bg-workbench-hover disabled:opacity-30 text-neutral-300 px-3 py-1.5 rounded text-xs transition-colors border border-neutral-700 flex items-center min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto justify-center"
          aria-label="Add direction"
        >
          <Plus className="w-3.5 h-3.5" />
        </button>
      </div>
      <div className="mt-1.5 bg-workbench-base border border-neutral-700 rounded px-2 py-1.5 flex flex-wrap gap-1 items-center workbench-focus">
        {form.newPathTags.map(tag => (
          <span key={tag} className="bg-workbench-panel border border-neutral-700 text-neutral-300 text-ui-sm font-mono px-1.5 py-0.5 rounded flex items-center gap-1">
            {tag}
            <button
              type="button"
              onClick={() => form.setNewPathTags(prev => prev.filter(t => t !== tag))}
              className="text-neutral-500 hover:text-neutral-300 transition-colors leading-none min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
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
          className="flex-1 min-w-[120px] bg-transparent text-neutral-100 text-xs placeholder-neutral-600 workbench-focus"
        />
      </div>
      <textarea
        value={form.newCompletionCriteria}
        onChange={e => form.setNewCompletionCriteria(e.target.value)}
        placeholder="Done when..."
        rows={2}
        className="mt-1.5 w-full bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs placeholder-neutral-600 resize-y workbench-focus"
      />
      {/* Acceptance-evidence contract: the same success_evidence /
          retirement_condition fields the system expects directions to carry,
          authored directly. 500-char limit mirrors the backend create endpoint. */}
      <label className="block mt-1.5 text-ui-sm text-neutral-500">Success evidence</label>
      <textarea
        value={form.newSuccessEvidence}
        onChange={e => form.setNewSuccessEvidence(e.target.value)}
        placeholder="Success evidence (what observable outcome proves this direction succeeded)..."
        rows={2}
        maxLength={500}
        className="mt-0.5 w-full bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs placeholder-neutral-600 resize-y workbench-focus"
      />
      <label className="block mt-1.5 text-ui-sm text-neutral-500">Retirement condition</label>
      <textarea
        value={form.newRetirementCondition}
        onChange={e => form.setNewRetirementCondition(e.target.value)}
        placeholder="Retirement condition (when this direction can be retired)..."
        rows={2}
        maxLength={500}
        className="mt-0.5 w-full bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs placeholder-neutral-600 resize-y workbench-focus"
      />
      {/* Born-staged create: stage the new direction for the next version in a
          single create call. Disabled (and explained) when no version is open to
          stage against, mirroring the edit-form "Staged" status option. */}
      <label
        className={`mt-1.5 flex items-center gap-2 text-xs ${canStage ? 'text-neutral-300 cursor-pointer' : 'text-neutral-600 cursor-not-allowed'}`}
        title={canStage
          ? `Create as staged to begin when v${stageTargetVersion} promotes and the next version opens`
          : 'No open release version to stage against'}
      >
        <input
          type="checkbox"
          checked={staged}
          disabled={!canStage}
          onChange={e => form.setNewStaged(e.target.checked)}
          className="accent-sky-500 disabled:opacity-40"
        />
        Stage for next version
      </label>
      {!canStage ? (
        <p className="mt-1 text-ui-sm text-neutral-500 leading-relaxed">
          No open release version to stage against.
        </p>
      ) : staged && (
        <p className="mt-1 text-ui-sm text-sky-400/80 leading-relaxed">
          Will stage to begin after v{stageTargetVersion} promotes and the next version opens.
        </p>
      )}
    </div>
  );
}
