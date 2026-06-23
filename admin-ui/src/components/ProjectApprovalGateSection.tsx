import { useEffect, useMemo, useState } from 'react';
import { validateApprovalGatePath } from '../api';

export interface ApprovalGateConfig {
  infra_files: string[];
  critical_files: string[];
}

interface ApprovalGateData {
  approval_gate_config: ApprovalGateConfig;
  /** True while both lists are still at the empty onboarding default. */
  is_default: boolean;
}

interface ProjectApprovalGateSectionProps {
  data: ApprovalGateData | null;
  canSave: boolean;
  onSave: (config: ApprovalGateConfig) => Promise<void>;
}

// Path validation mirrors backend _validate_approval_gate_paths in
// admin_project_config.py (repo-relative, no leading slash, no parent-directory
// traversal, no shell metacharacters). The single source of truth lives in
// api.ts:validateApprovalGatePath so the editor and any future caller stay in
// lockstep with the server rules.
const pathError = validateApprovalGatePath;

const LIST_META: { key: keyof ApprovalGateConfig; label: string; hint: string }[] = [
  { key: 'infra_files', label: 'Infra files', hint: 'PRs touching these escalate reviewer effort and require human approval to merge.' },
  { key: 'critical_files', label: 'Critical files', hint: 'High-stakes paths that also gate the merge on human approval.' },
];

function PathListEditor({
  label,
  hint,
  paths,
  canEdit,
  onChange,
}: {
  label: string;
  hint: string;
  paths: string[];
  canEdit: boolean;
  onChange: (next: string[]) => void;
}) {
  const update = (i: number, value: string) => {
    const next = paths.slice();
    next[i] = value;
    onChange(next);
  };
  const remove = (i: number) => onChange(paths.filter((_, idx) => idx !== i));
  const add = () => onChange([...paths, '']);

  return (
    <div>
      <h4 className="text-xs font-semibold text-neutral-300 mb-1">{label}</h4>
      <p className="text-ui-md text-neutral-600 mb-2">{hint}</p>
      <div className="space-y-2">
        {paths.length === 0 && (
          <p className="text-ui-md text-neutral-500 italic">No paths configured.</p>
        )}
        {paths.map((path, i) => {
          const err = path.trim() === '' && paths.length === 1 ? null : pathError(path);
          return (
            <div key={i}>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={path}
                  disabled={!canEdit}
                  onChange={e => update(i, e.target.value)}
                  placeholder="e.g. backend/app/auth.py"
                  className={`flex-1 min-w-0 bg-neutral-950 border ${err ? 'border-red-600' : 'border-neutral-700'} text-neutral-100 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 disabled:opacity-50`}
                />
                {canEdit && (
                  <button
                    onClick={() => remove(i)}
                    aria-label={`Remove ${label} entry`}
                    className="inline-flex items-center justify-center min-h-[44px] min-w-[44px] bg-neutral-800 hover:bg-neutral-700 text-neutral-400 rounded-lg text-xs transition-colors shrink-0"
                  >
                    ✕
                  </button>
                )}
              </div>
              {err && <p className="text-ui-md text-red-400 mt-1">{err}</p>}
            </div>
          );
        })}
      </div>
      {canEdit && (
        <button
          onClick={add}
          className="inline-flex items-center justify-center min-h-[44px] mt-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 rounded-lg px-4 py-2 text-xs transition-colors"
        >
          + Add path
        </button>
      )}
    </div>
  );
}

export function ProjectApprovalGateSection({ data, canSave, onSave }: ProjectApprovalGateSectionProps) {
  const [infra, setInfra] = useState<string[]>(data?.approval_gate_config.infra_files ?? []);
  const [critical, setCritical] = useState<string[]>(data?.approval_gate_config.critical_files ?? []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Collapsed until the operator opts in; auto-expands below when files exist.
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    setInfra(data?.approval_gate_config.infra_files ?? []);
    setCritical(data?.approval_gate_config.critical_files ?? []);
    setExpanded(false);
  }, [data]);

  const setters: Record<keyof ApprovalGateConfig, (next: string[]) => void> = {
    infra_files: next => { setInfra(next); setError(null); },
    critical_files: next => { setCritical(next); setError(null); },
  };
  const values: ApprovalGateConfig = { infra_files: infra, critical_files: critical };

  const hasInvalid = useMemo(
    () => [...infra, ...critical].some(p => pathError(p) !== null),
    [infra, critical],
  );
  const hasChanges =
    JSON.stringify({ i: infra, c: critical }) !==
    JSON.stringify({
      i: data?.approval_gate_config.infra_files ?? [],
      c: data?.approval_gate_config.critical_files ?? [],
    });

  const handleSave = async () => {
    if (hasInvalid) return;
    setSaving(true);
    setError(null);
    try {
      await onSave({
        infra_files: infra.map(p => p.trim()).filter(Boolean),
        critical_files: critical.map(p => p.trim()).filter(Boolean),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const isEmpty = infra.length === 0 && critical.length === 0;

  // Empty / onboarding is a first-class, accepted state — a freshly onboarded
  // repo legitimately has no approval-gate files yet. Collapse to a single line
  // plus an opt-in button so it doesn't dominate the settings panel; expand only
  // when the project already has files or the operator chooses to add some.
  if (isEmpty && !expanded) {
    return (
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider">
            Approval-Gate Files
            <span className="ml-2 text-ui-sm font-normal normal-case tracking-normal text-neutral-600">optional · none set</span>
          </h3>
          <p className="text-ui-md text-neutral-600 mt-0.5">
            PRs touching configured paths require human approval — add some when you onboard a repo.
          </p>
        </div>
        {canSave && (
          <button
            onClick={() => setExpanded(true)}
            className="inline-flex items-center justify-center min-h-[44px] bg-neutral-800 hover:bg-neutral-700 text-neutral-300 rounded-lg px-4 py-2 text-xs transition-colors shrink-0"
          >
            + Add files
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-2">Approval-Gate Files</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          PRs touching these repo-relative paths require human approval to merge. Empty is fine — changes take effect immediately.
        </p>

        <div className="space-y-4">
          {LIST_META.map(meta => (
            <PathListEditor
              key={meta.key}
              label={meta.label}
              hint={meta.hint}
              paths={values[meta.key]}
              canEdit={canSave}
              onChange={setters[meta.key]}
            />
          ))}
        </div>
      </div>

      {error && <p className="text-ui-md text-red-400">{error}</p>}

      <div className="flex items-center justify-between pt-1 gap-3 flex-wrap">
        {canSave ? (
          <button
            onClick={handleSave}
            disabled={saving || !hasChanges || hasInvalid}
            className="inline-flex items-center justify-center min-h-[44px] bg-neutral-700 hover:bg-neutral-600 text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50 ml-auto"
          >
            {saving ? 'Saving...' : 'Save Approval-Gate Files'}
          </button>
        ) : (
          <p className="text-xs text-neutral-500">Only project owners can change the approval-gate file lists.</p>
        )}
      </div>
    </div>
  );
}
