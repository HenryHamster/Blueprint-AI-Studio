/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useState } from 'react';

interface IntegrationBranchData {
  integration_branch: string;
  is_default: boolean;
}

interface ProjectIntegrationBranchSectionProps {
  data: IntegrationBranchData | null;
  canSave: boolean;
  onSave: (branch: string | null) => Promise<void>;
  /** When provided and different from integration_branch, displays LTS workflow context. */
  relatedPromotionTarget?: string;
}

const VALID_BRANCH_RE = /^[a-zA-Z0-9._/-]+$/;

export function ProjectIntegrationBranchSection({ data, canSave, onSave, relatedPromotionTarget }: ProjectIntegrationBranchSectionProps) {
  const [branchInput, setBranchInput] = useState(data?.integration_branch ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setBranchInput(data?.integration_branch ?? '');
  }, [data]);

  const isValid = branchInput.trim() === '' || VALID_BRANCH_RE.test(branchInput.trim());
  const hasChanges = branchInput.trim() !== (data?.integration_branch ?? '');

  const handleSave = async () => {
    const branch = branchInput.trim();
    if (!branch) return;
    if (!VALID_BRANCH_RE.test(branch)) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(branch);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const handleRevert = async () => {
    setSaving(true);
    setError(null);
    try {
      await onSave(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Revert failed');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Integration Branch</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          The trunk branch autonomous tasks target for spawning and merging. Overrides the global default for this project. For LTS workflows, pair with a distinct Promotion Target.
        </p>

        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <span className="text-xs text-neutral-400 shrink-0 w-24">Current value</span>
            <span className="font-mono text-xs text-neutral-200 bg-workbench-base border border-neutral-800 rounded px-2 py-1 flex-1 truncate">
              {data?.integration_branch ?? '—'}
            </span>
            {data?.is_default ? (
              <span className="text-ui-sm uppercase tracking-wide px-1.5 py-0.5 rounded bg-workbench-panel text-neutral-500 border border-neutral-700 shrink-0">global default</span>
            ) : (
              <span className="text-ui-sm uppercase tracking-wide px-1.5 py-0.5 rounded bg-cyan-950/40 text-cyan-400 border border-cyan-900/50 shrink-0">override</span>
            )}
          </div>

          {relatedPromotionTarget && relatedPromotionTarget !== (data?.integration_branch ?? '') && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-neutral-400 shrink-0 w-24">Promotion target</span>
              <span className="font-mono text-xs text-neutral-300 bg-workbench-base border border-neutral-800 rounded px-2 py-1 flex-1 truncate">
                {relatedPromotionTarget}
              </span>
              <span className="text-ui-sm uppercase tracking-wide px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-400 border border-amber-900/50 shrink-0">LTS</span>
            </div>
          )}

          <div>
            <label className="block text-xs text-neutral-400 mb-1">
              New branch name <span className="text-neutral-600">(a–z, A–Z, 0–9, . _ / -)</span>
            </label>
            <input
              type="text"
              value={branchInput}
              onChange={e => { setBranchInput(e.target.value); setError(null); }}
              placeholder={data?.integration_branch ?? 'e.g. main/master/trunk'}
              className={`w-full bg-workbench-shell border ${isValid ? 'border-neutral-700' : 'border-red-600'} text-neutral-100 rounded-lg px-3 py-2 text-xs font-mono workbench-focus`}
            />
            {!isValid && (
              <p className="text-ui-md text-red-400 mt-1">Branch name contains invalid characters. Use only a–z, A–Z, 0–9, ., _, /, -.</p>
            )}
          </div>
        </div>
      </div>

      {error && <p className="text-ui-md text-red-400">{error}</p>}

      <div className="flex items-center justify-between pt-2 gap-3 flex-wrap">
        {canSave ? (
          <>
            {!data?.is_default && (
              <button
                onClick={handleRevert}
                disabled={saving}
                className="inline-flex items-center justify-center min-h-[44px] bg-workbench-panel hover:bg-workbench-hover text-neutral-400 rounded-lg px-4 py-2 text-xs transition-colors disabled:opacity-50"
              >
                {saving ? 'Reverting...' : 'Revert to default'}
              </button>
            )}
            <button
              onClick={handleSave}
              disabled={saving || !hasChanges || !isValid || !branchInput.trim()}
              className="inline-flex items-center justify-center min-h-[44px] bg-workbench-layer hover:bg-workbench-hover text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50 ml-auto"
            >
              {saving ? 'Saving...' : 'Save Integration Branch'}
            </button>
          </>
        ) : (
          <p className="text-xs text-neutral-500">Only project owners can change the integration branch.</p>
        )}
      </div>
    </div>
  );
}
