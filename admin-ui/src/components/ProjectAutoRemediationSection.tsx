/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import type { AutoRemediationConfig, AutoRemediationStats } from '../types/project';

interface ProjectAutoRemediationSectionProps {
  githubOwner: string | null;
  githubRepo: string | null;
  config: AutoRemediationConfig | null;
  stats: AutoRemediationStats | null;
  onSave: (config: AutoRemediationConfig) => Promise<void>;
  integrationBranch?: string | null;
  promotionTarget?: string | null;
  canSave?: boolean;
}

const DEFAULT_CONFIG_BASE: Omit<AutoRemediationConfig, 'branch_whitelist'> = {
  enabled: false,
  max_per_hour: 2,
  max_concurrent: 2,
  require_review: true,
};

function defaultBranchWhitelist(integrationBranch?: string | null, promotionTarget?: string | null): string[] {
  return [integrationBranch, promotionTarget].filter((b): b is string => Boolean(b));
}

const STATUS_BADGE: Record<string, string> = {
  COMPLETED: 'bg-emerald-900/50 text-emerald-400',
  FAILED: 'bg-red-900/50 text-red-400',
  IN_PROGRESS: 'bg-blue-900/50 text-blue-400',
  EVALUATING: 'bg-blue-900/50 text-blue-400',
  QUEUED: 'bg-yellow-900/50 text-yellow-400',
  PROVISIONING: 'bg-yellow-900/50 text-yellow-400',
  CANCELLED: 'bg-neutral-800 text-neutral-500',
};

export function ProjectAutoRemediationSection({
  githubOwner,
  githubRepo,
  config,
  stats,
  onSave,
  integrationBranch,
  promotionTarget,
  canSave = false,
}: ProjectAutoRemediationSectionProps) {
  const defaultWhitelist = defaultBranchWhitelist(integrationBranch, promotionTarget);
  const effective = config ?? { ...DEFAULT_CONFIG_BASE, branch_whitelist: defaultWhitelist };

  const [enabled, setEnabled] = useState(effective.enabled);
  const [branchWhitelist, setBranchWhitelist] = useState<string[]>(effective.branch_whitelist);
  const [maxPerHour, setMaxPerHour] = useState(effective.max_per_hour);
  const [maxConcurrent, setMaxConcurrent] = useState(effective.max_concurrent);
  const [requireReview, setRequireReview] = useState(effective.require_review);
  const [newBranch, setNewBranch] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const e = config ?? { ...DEFAULT_CONFIG_BASE, branch_whitelist: defaultBranchWhitelist(integrationBranch, promotionTarget) };
    setEnabled(e.enabled);
    setBranchWhitelist(e.branch_whitelist);
    setMaxPerHour(e.max_per_hour);
    setMaxConcurrent(e.max_concurrent);
    setRequireReview(e.require_review);
  }, [config, integrationBranch, promotionTarget]);

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    try {
      await onSave({
        enabled,
        branch_whitelist: branchWhitelist,
        max_per_hour: maxPerHour,
        max_concurrent: maxConcurrent,
        require_review: requireReview,
      });
    } finally {
      setSaving(false);
    }
  };

  const addBranch = () => {
    const trimmed = newBranch.trim();
    if (trimmed && !branchWhitelist.includes(trimmed)) {
      setBranchWhitelist(prev => [...prev, trimmed]);
    }
    setNewBranch('');
  };

  const removeBranch = (branch: string) => {
    setBranchWhitelist(prev => prev.filter(b => b !== branch));
  };

  return (
    <div className="space-y-6">
      <fieldset disabled={!canSave} className="contents">
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
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Auto-Remediation</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          When enabled, Blueprint automatically opens fix PRs for CI failures on protected branches.
        </p>
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3 flex items-center justify-between">
          <span className="text-xs text-neutral-300">Enable auto-remediation</span>
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

      {/* Branch whitelist */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-2">Protected Branches</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          Auto-remediation only triggers for CI failures on these branches.
        </p>
        <div className="space-y-1.5 mb-2">
          {branchWhitelist.map(branch => (
            <div key={branch} className="flex items-center gap-2 bg-neutral-900 border border-neutral-800 rounded px-3 py-1.5">
              <span className="text-xs font-mono text-neutral-200 flex-1">{branch}</span>
              {/* reason: small icon-only delete button — operator-density convention for settings panels */}
              <button
                data-viewport-allow-small-target
                onClick={() => removeBranch(branch)}
                className="text-neutral-600 hover:text-red-400 transition-colors"
                title="Remove branch"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={newBranch}
            onChange={e => setNewBranch(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') addBranch(); }}
            placeholder="branch-name"
            className="flex-1 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
          />
          {/* reason: dense-UI inline +Add control — operator-density convention for settings panels */}
          <button
            data-viewport-allow-small-target
            onClick={addBranch}
            disabled={!newBranch.trim()}
            className="flex items-center gap-1 text-xs text-neutral-500 hover:text-neutral-300 transition-colors disabled:opacity-40"
          >
            <Plus className="w-3.5 h-3.5" />
            Add
          </button>
        </div>
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

      {/* Require review */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Review</h3>
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3 flex items-center justify-between">
          <div>
            <span className="text-xs text-neutral-300">Require human review before merge</span>
            <p className="text-ui-md text-neutral-600 mt-0.5">Auto-remediation PRs will wait for approval before auto-merging.</p>
          </div>
          {/* reason: dense-UI toggle (h-5 w-9) — operator-density convention for settings panels */}
          <button
            data-viewport-allow-small-target
            onClick={() => setRequireReview(v => !v)}
            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ml-4 ${requireReview ? 'bg-emerald-600' : 'bg-neutral-700'}`}
          >
            <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${requireReview ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
          </button>
        </div>
      </div>

      {/* Activity stats */}
      {stats && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Activity</h3>
          <div className="grid grid-cols-3 gap-2 mb-3">
            <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-center">
              <div className="text-lg font-semibold text-neutral-100">{stats.spawned_24h}</div>
              <div className="text-ui-sm text-neutral-500">spawned 24h</div>
            </div>
            <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-center">
              <div className="text-lg font-semibold text-neutral-100">{stats.spawned_7d}</div>
              <div className="text-ui-sm text-neutral-500">spawned 7d</div>
            </div>
            <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-center">
              <div className="text-lg font-semibold text-neutral-100">
                {stats.success_rate !== null ? `${stats.success_rate}%` : '—'}
              </div>
              <div className="text-ui-sm text-neutral-500">success rate</div>
            </div>
          </div>
          {stats.recent_tasks.length > 0 && (
            <div className="space-y-1">
              {stats.recent_tasks.map(t => (
                <div key={t.id} className="flex items-center gap-2 bg-neutral-900 border border-neutral-800 rounded px-3 py-1.5">
                  <span className={`text-ui-sm font-medium px-1.5 py-0.5 rounded ${STATUS_BADGE[t.status] ?? 'bg-neutral-800 text-neutral-400'}`}>
                    {t.status}
                  </span>
                  <span className="text-xs font-mono text-neutral-400 truncate flex-1">{t.branch || t.title}</span>
                </div>
              ))}
            </div>
          )}
          {stats.spawned_7d === 0 && (
            <p className="text-ui-md text-neutral-600">No auto-remediation tasks in the last 7 days.</p>
          )}
        </div>
      )}

      {/* Save button */}
      <div className="flex justify-end pt-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="inline-flex items-center justify-center min-h-[44px] bg-neutral-700 hover:bg-neutral-600 text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Auto-Remediation Config'}
        </button>
      </div>
      </fieldset>
      {!canSave && (
        <p className="text-right text-xs text-neutral-500">Project owner access is required to change auto-remediation settings.</p>
      )}
    </div>
  );
}
