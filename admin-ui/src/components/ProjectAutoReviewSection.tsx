/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useMemo, useState } from 'react';
import type { AutoReviewConfig } from '../types/project';

interface ProjectAutoReviewSectionProps {
  config: AutoReviewConfig | null;
  canSave: boolean;
  onSave: (patch: Partial<AutoReviewConfig>) => Promise<void>;
}

const DEFAULT_CONFIG: AutoReviewConfig = {
  enabled: false,
  max_per_hour: 5,
  max_concurrent: 3,
};

export function ProjectAutoReviewSection({ config, canSave, onSave }: ProjectAutoReviewSectionProps) {
  const effective = config ?? DEFAULT_CONFIG;
  const [enabled, setEnabled] = useState(effective.enabled);
  const [maxPerHourInput, setMaxPerHourInput] = useState(String(effective.max_per_hour));
  const [maxConcurrentInput, setMaxConcurrentInput] = useState(String(effective.max_concurrent));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const next = config ?? DEFAULT_CONFIG;
    setEnabled(next.enabled);
    setMaxPerHourInput(String(next.max_per_hour));
    setMaxConcurrentInput(String(next.max_concurrent));
  }, [config]);

  const maxPerHour = Number(maxPerHourInput);
  const maxConcurrent = Number(maxConcurrentInput);
  const maxPerHourValid = Number.isInteger(maxPerHour) && maxPerHour >= 1 && maxPerHour <= 20;
  const maxConcurrentValid = Number.isInteger(maxConcurrent) && maxConcurrent >= 1 && maxConcurrent <= 10;
  const hasChanges = useMemo(() => (
    enabled !== effective.enabled ||
    maxPerHour !== effective.max_per_hour ||
    maxConcurrent !== effective.max_concurrent
  ), [enabled, maxPerHour, maxConcurrent, effective.enabled, effective.max_per_hour, effective.max_concurrent]);

  const handleSave = async () => {
    const patch: Partial<AutoReviewConfig> = {};
    if (enabled !== effective.enabled) patch.enabled = enabled;
    if (maxPerHour !== effective.max_per_hour) patch.max_per_hour = maxPerHour;
    if (maxConcurrent !== effective.max_concurrent) patch.max_concurrent = maxConcurrent;
    if (Object.keys(patch).length === 0) return;

    setSaving(true);
    try {
      await onSave(patch);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Auto-Review</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          Automatically enqueue review tasks for incoming changes on this project.
        </p>
        <div className="bg-workbench-base border border-neutral-800 rounded-lg px-4 py-3 flex items-center justify-between">
          <span className="text-xs text-neutral-300">Enable auto-review</span>
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

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Rate Limits</h3>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-neutral-400 mb-1">Max per hour <span className="text-neutral-600">(1–20)</span></label>
            <input
              type="number"
              min={1}
              max={20}
              value={maxPerHourInput}
              onChange={e => setMaxPerHourInput(e.target.value)}
              className={`w-full bg-workbench-shell border ${maxPerHourValid ? 'border-neutral-700' : 'border-red-600'} text-neutral-100 rounded-lg px-3 py-2 text-xs workbench-focus`}
            />
            {!maxPerHourValid && <p className="text-ui-md text-red-400 mt-1">Enter an integer between 1 and 20.</p>}
          </div>
          <div>
            <label className="block text-xs text-neutral-400 mb-1">Max concurrent <span className="text-neutral-600">(1–10)</span></label>
            <input
              type="number"
              min={1}
              max={10}
              value={maxConcurrentInput}
              onChange={e => setMaxConcurrentInput(e.target.value)}
              className={`w-full bg-workbench-shell border ${maxConcurrentValid ? 'border-neutral-700' : 'border-red-600'} text-neutral-100 rounded-lg px-3 py-2 text-xs workbench-focus`}
            />
            {!maxConcurrentValid && <p className="text-ui-md text-red-400 mt-1">Enter an integer between 1 and 10.</p>}
          </div>
        </div>
      </div>

      <div className="flex justify-end pt-2">
        {canSave ? (
          <button
            onClick={handleSave}
            disabled={saving || !hasChanges || !maxPerHourValid || !maxConcurrentValid}
            className="inline-flex items-center justify-center min-h-[44px] bg-workbench-layer hover:bg-workbench-hover text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50"
          >
            {saving ? 'Saving...' : 'Save Auto-Review Config'}
          </button>
        ) : (
          <p className="text-xs text-neutral-500">Only project owners can save auto-review settings.</p>
        )}
      </div>
    </div>
  );
}
