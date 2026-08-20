/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect } from 'react';
import type { IssueImplementationConfig, IssueImplementationConfigUpdate } from '../types/project';

interface ProjectIssueImplementationSectionProps {
  githubOwner: string | null;
  githubRepo: string | null;
  config: IssueImplementationConfig | null;
  onSave: (config: IssueImplementationConfigUpdate) => Promise<void>;
  canSave?: boolean;
}

const DEFAULT_CONFIG: IssueImplementationConfig = {
  enabled: false,
  trigger_label: 'auto:implement',
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
  canSave = false,
}: ProjectIssueImplementationSectionProps) {
  const effective = config ?? DEFAULT_CONFIG;

  const [enabled, setEnabled] = useState(effective.enabled);
  const [triggerLabel, setTriggerLabel] = useState(effective.trigger_label);
  const [maxPerHour, setMaxPerHour] = useState(effective.max_per_hour);
  const [maxConcurrent, setMaxConcurrent] = useState(effective.max_concurrent);
  const [defaultEffort, setDefaultEffort] = useState<'low' | 'medium' | 'high'>(effective.default_effort);
  const [saving, setSaving] = useState(false);

  // base_branch is server-derived from project.integration_branch — display only, never edited here.
  const baseBranchDisplay = effective.base_branch || '—';

  useEffect(() => {
    const e = config ?? DEFAULT_CONFIG;
    setEnabled(e.enabled);
    setTriggerLabel(e.trigger_label);
    setMaxPerHour(e.max_per_hour);
    setMaxConcurrent(e.max_concurrent);
    setDefaultEffort(e.default_effort);
  }, [config]);

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    try {
      await onSave({
        enabled,
        trigger_label: triggerLabel,
        max_per_hour: maxPerHour,
        max_concurrent: maxConcurrent,
        default_effort: defaultEffort,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <fieldset disabled={!canSave} className="contents">
      {/* Repository info */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Repository</h3>
        <div className="bg-workbench-base border border-neutral-800 rounded-lg px-4 py-3 flex items-center gap-2">
          <span className="text-xs text-neutral-400">Repository</span>
          <span className="text-xs text-neutral-100 font-mono ml-auto">
            {githubOwner && githubRepo ? `${githubOwner}/${githubRepo}` : '—'}
          </span>
        </div>
      </div>

      {/* Enable/disable toggle */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Issue Implementation</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          When enabled, Blueprint automatically spawns an implementation task when a GitHub issue is labeled with the trigger label.
        </p>
        <div className="bg-workbench-base border border-neutral-800 rounded-lg px-4 py-3 flex items-center justify-between">
          <span className="text-xs text-neutral-300">Enable issue-to-implementation</span>
          {/* reason: dense-UI toggle (h-5 w-9) — operator-density convention for settings panels */}
          <button
            data-viewport-allow-small-target
            onClick={() => setEnabled(v => !v)}
            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ${enabled ? 'bg-emerald-600' : 'bg-workbench-layer'}`}
          >
            <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
          </button>
        </div>
      </div>

      {/* Trigger label */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-2">Trigger Label</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          GitHub issues labeled with this label will trigger Blueprint to create an implementation task.
        </p>
        <input
          type="text"
          value={triggerLabel}
          onChange={e => setTriggerLabel(e.target.value)}
          placeholder="auto:implement"
          className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs font-mono workbench-focus"
        />
      </div>

      {/* Base branch — read-only; derived live from the project's integration branch */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-2">Base Branch</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          Implementation PRs are based on the project's integration branch. Change it in the Integration Branch section.
        </p>
        <div className="bg-workbench-base border border-neutral-800 rounded-lg px-4 py-3 flex items-center gap-2">
          <span className="text-xs text-neutral-400">Integration branch</span>
          <span className="text-xs text-neutral-100 font-mono ml-auto">{baseBranchDisplay}</span>
        </div>
      </div>

      {/* Default effort */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Default Effort</h3>
        <select
          value={defaultEffort}
          onChange={e => setDefaultEffort(e.target.value as 'low' | 'medium' | 'high')}
          className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs workbench-focus"
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
              className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs workbench-focus"
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
              className="w-full bg-workbench-shell border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs workbench-focus"
            />
          </div>
        </div>
      </div>

      {/* Save button */}
      <div className="flex justify-end pt-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="inline-flex items-center justify-center min-h-[44px] bg-workbench-layer hover:bg-workbench-hover text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Issue Implementation Config'}
        </button>
      </div>
      </fieldset>
      {!canSave && (
        <p className="text-right text-xs text-neutral-500">Project owner access is required to change issue implementation settings.</p>
      )}
    </div>
  );
}
