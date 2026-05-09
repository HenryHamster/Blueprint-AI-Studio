import { useState, useEffect } from 'react';
import type { IssueImplementationConfig } from '../types/project';

interface ProjectIssueImplementationSectionProps {
  githubOwner: string | null;
  githubRepo: string | null;
  config: IssueImplementationConfig | null;
  onSave: (config: IssueImplementationConfig) => Promise<void>;
}

const DEFAULT_CONFIG: IssueImplementationConfig = {
  enabled: false,
  trigger_label: 'blueprint:implement',
  max_per_hour: 2,
  max_concurrent: 2,
  base_branch: '',
  default_effort: 'medium',
};

export function ProjectIssueImplementationSection({
  githubOwner,
  githubRepo,
  config,
  onSave,
}: ProjectIssueImplementationSectionProps) {
  const effective = config ?? DEFAULT_CONFIG;

  const [enabled, setEnabled] = useState(effective.enabled);
  const [triggerLabel, setTriggerLabel] = useState(effective.trigger_label);
  const [maxPerHour, setMaxPerHour] = useState(effective.max_per_hour);
  const [maxConcurrent, setMaxConcurrent] = useState(effective.max_concurrent);
  const [baseBranch, setBaseBranch] = useState(effective.base_branch);
  const [defaultEffort, setDefaultEffort] = useState<'low' | 'medium' | 'high'>(effective.default_effort);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const e = config ?? DEFAULT_CONFIG;
    setEnabled(e.enabled);
    setTriggerLabel(e.trigger_label);
    setMaxPerHour(e.max_per_hour);
    setMaxConcurrent(e.max_concurrent);
    setBaseBranch(e.base_branch);
    setDefaultEffort(e.default_effort);
  }, [config]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave({
        enabled,
        trigger_label: triggerLabel,
        max_per_hour: maxPerHour,
        max_concurrent: maxConcurrent,
        base_branch: baseBranch,
        default_effort: defaultEffort,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Repository info */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Repository</h3>
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3 flex items-center gap-2">
          <span className="text-xs text-neutral-400">Repository</span>
          <span className="text-xs text-neutral-100 font-mono ml-auto">
            {githubOwner && githubRepo ? `${githubOwner}/${githubRepo}` : '—'}
          </span>
        </div>
      </div>

      {/* Enable/disable toggle */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Issue Implementation</h3>
        <p className="text-[11px] text-neutral-600 mb-3">
          When enabled, Blueprint automatically spawns an implementation task when a GitHub issue is labeled with the trigger label.
        </p>
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3 flex items-center justify-between">
          <span className="text-xs text-neutral-300">Enable issue-to-implementation</span>
          {/* reason: dense-UI toggle (h-5 w-9) — operator-density convention for settings panels */}
          <button
            data-viewport-allow-small-target
            onClick={() => setEnabled(v => !v)}
            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ${enabled ? 'bg-emerald-600' : 'bg-neutral-700'}`}
          >
            <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
          </button>
        </div>
      </div>

      {/* Trigger label */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-2">Trigger Label</h3>
        <p className="text-[11px] text-neutral-600 mb-3">
          GitHub issues labeled with this label will trigger Blueprint to create an implementation task.
        </p>
        <input
          type="text"
          value={triggerLabel}
          onChange={e => setTriggerLabel(e.target.value)}
          placeholder="blueprint:implement"
          className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
        />
      </div>

      {/* Base branch */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-2">Base Branch</h3>
        <p className="text-[11px] text-neutral-600 mb-3">
          Branch to base implementation PRs on. Leave empty to use the repository default branch.
        </p>
        <input
          type="text"
          value={baseBranch}
          onChange={e => setBaseBranch(e.target.value)}
          placeholder="main"
          className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
        />
      </div>

      {/* Default effort */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Default Effort</h3>
        <select
          value={defaultEffort}
          onChange={e => setDefaultEffort(e.target.value as 'low' | 'medium' | 'high')}
          className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
        >
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </div>

      {/* Rate limits */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Rate Limits</h3>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-neutral-400 mb-1">Max per hour <span className="text-neutral-600">(1–10)</span></label>
            <input
              type="number"
              min={1}
              max={10}
              value={maxPerHour}
              onChange={e => setMaxPerHour(Math.max(1, Math.min(10, parseInt(e.target.value) || 1)))}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs text-neutral-400 mb-1">Max concurrent <span className="text-neutral-600">(1–5)</span></label>
            <input
              type="number"
              min={1}
              max={5}
              value={maxConcurrent}
              onChange={e => setMaxConcurrent(Math.max(1, Math.min(5, parseInt(e.target.value) || 1)))}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
        </div>
      </div>

      {/* Save button */}
      <div className="flex justify-end pt-2">
        {/* reason: dense-UI Save button (text-xs py-2) — operator-density convention for settings panels */}
        <button
          data-viewport-allow-small-target
          onClick={handleSave}
          disabled={saving}
          className="bg-neutral-700 hover:bg-neutral-600 text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Issue Implementation Config'}
        </button>
      </div>
    </div>
  );
}
