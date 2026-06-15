import { useEffect, useMemo, useState } from 'react';
import type { ProjectResourceEnvelopeResponse } from '../types/project';
import type { ResourceEnvelopePatch } from '../api';

interface ProjectResourceEnvelopeSectionProps {
  githubOwner: string | null;
  githubRepo: string | null;
  data: ProjectResourceEnvelopeResponse | null;
  loading: boolean;
  error: string | null;
  canSave: boolean;
  onSave: (patch: ResourceEnvelopePatch) => Promise<void>;
}

function formatUsd(value: number | null | undefined): string {
  return value == null ? 'Unlimited' : `$${value.toFixed(2)}`;
}

function formatCount(value: number | null | undefined): string {
  return value == null ? 'Unlimited' : String(value);
}

export function ProjectResourceEnvelopeSection({
  githubOwner,
  githubRepo,
  data,
  loading,
  error,
  canSave,
  onSave,
}: ProjectResourceEnvelopeSectionProps) {
  const [maxConcurrentVmsInput, setMaxConcurrentVmsInput] = useState('');
  const [spendCeilingUsdInput, setSpendCeilingUsdInput] = useState('');
  const [spendWindowDaysInput, setSpendWindowDaysInput] = useState('30');
  const [maxConcurrentComputeJobsInput, setMaxConcurrentComputeJobsInput] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setMaxConcurrentVmsInput(data?.max_concurrent_vms != null ? String(data.max_concurrent_vms) : '');
    setSpendCeilingUsdInput(data?.spend_ceiling_usd != null ? String(data.spend_ceiling_usd) : '');
    setSpendWindowDaysInput(String(data?.spend_window_days ?? 30));
    setMaxConcurrentComputeJobsInput(data?.max_concurrent_compute_jobs != null ? String(data.max_concurrent_compute_jobs) : '');
  }, [data]);

  const parsedMaxConcurrent = useMemo(() => {
    if (!maxConcurrentVmsInput.trim()) return null;
    const parsed = Number(maxConcurrentVmsInput);
    return Number.isInteger(parsed) && parsed >= 1 ? parsed : Number.NaN;
  }, [maxConcurrentVmsInput]);

  const parsedSpendCeiling = useMemo(() => {
    if (!spendCeilingUsdInput.trim()) return null;
    const parsed = Number(spendCeilingUsdInput);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : Number.NaN;
  }, [spendCeilingUsdInput]);

  const parsedSpendWindowDays = useMemo(() => {
    const parsed = Number(spendWindowDaysInput);
    return Number.isInteger(parsed) && parsed >= 1 ? parsed : Number.NaN;
  }, [spendWindowDaysInput]);

  const parsedMaxConcurrentComputeJobs = useMemo(() => {
    if (!maxConcurrentComputeJobsInput.trim()) return null;
    const parsed = Number(maxConcurrentComputeJobsInput);
    return Number.isInteger(parsed) && parsed >= 1 ? parsed : Number.NaN;
  }, [maxConcurrentComputeJobsInput]);

  const current = data ?? {
    project_id: '',
    max_concurrent_vms: null,
    spend_ceiling_usd: null,
    spend_window_days: 30,
    max_concurrent_compute_jobs: null,
    active_vm_count: 0,
    recent_spend_usd: 0,
  };

  const currentCapLabel = formatCount(current.max_concurrent_vms);
  const currentCeilingLabel = formatUsd(current.spend_ceiling_usd);
  const activeOverCap =
    current.max_concurrent_vms != null && current.active_vm_count > current.max_concurrent_vms;
  const spendOverCeiling =
    current.spend_ceiling_usd != null && current.recent_spend_usd > current.spend_ceiling_usd;

  const hasChanges = useMemo(() => {
    const maxChanged = maxConcurrentVmsInput.trim()
      ? String(current.max_concurrent_vms ?? '') !== maxConcurrentVmsInput.trim()
      : current.max_concurrent_vms !== null;
    const spendChanged = spendCeilingUsdInput.trim()
      ? String(current.spend_ceiling_usd ?? '') !== spendCeilingUsdInput.trim()
      : current.spend_ceiling_usd !== null;
    const windowChanged = String(current.spend_window_days ?? 30) !== spendWindowDaysInput.trim();
    const computeJobsChanged = maxConcurrentComputeJobsInput.trim()
      ? String(current.max_concurrent_compute_jobs ?? '') !== maxConcurrentComputeJobsInput.trim()
      : current.max_concurrent_compute_jobs !== null;
    return maxChanged || spendChanged || windowChanged || computeJobsChanged;
  }, [
    current.max_concurrent_vms,
    current.spend_ceiling_usd,
    current.spend_window_days,
    current.max_concurrent_compute_jobs,
    maxConcurrentVmsInput,
    spendCeilingUsdInput,
    spendWindowDaysInput,
    maxConcurrentComputeJobsInput,
  ]);

  const handleSave = async () => {
    if (!canSave || Number.isNaN(parsedMaxConcurrent) || Number.isNaN(parsedSpendCeiling) || Number.isNaN(parsedSpendWindowDays) || Number.isNaN(parsedMaxConcurrentComputeJobs)) {
      return;
    }
    setSaving(true);
    try {
      await onSave({
        max_concurrent_vms: parsedMaxConcurrent,
        spend_ceiling_usd: parsedSpendCeiling,
        spend_window_days: parsedSpendWindowDays,
        max_concurrent_compute_jobs: parsedMaxConcurrentComputeJobs,
      });
    } finally {
      setSaving(false);
    }
  };

  const inputDisabled = !canSave || saving;
  const canSubmit = canSave && hasChanges && !Number.isNaN(parsedMaxConcurrent) && !Number.isNaN(parsedSpendCeiling) && !Number.isNaN(parsedSpendWindowDays) && !Number.isNaN(parsedMaxConcurrentComputeJobs);

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Repository</h3>
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3 flex items-center gap-2">
          <span className="text-xs text-neutral-400">Repository</span>
          <span className="text-xs text-neutral-100 font-mono ml-auto">
            {githubOwner && githubRepo ? `${githubOwner}/${githubRepo}` : '—'}
          </span>
        </div>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Configured Limits</h3>
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-[10px] uppercase tracking-wider text-neutral-500">Concurrent VMs</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{currentCapLabel}</div>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-[10px] uppercase tracking-wider text-neutral-500">Spend ceiling</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{currentCeilingLabel}</div>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-[10px] uppercase tracking-wider text-neutral-500">Spend window</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{current.spend_window_days} days</div>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-[10px] uppercase tracking-wider text-neutral-500">Concurrent compute jobs</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{formatCount(current.max_concurrent_compute_jobs)}</div>
          </div>
        </div>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Live Usage</h3>
        <div className="space-y-3">
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs text-neutral-300">Active tasks</span>
              <span className={`text-xs font-mono ${activeOverCap ? 'text-red-400' : 'text-neutral-100'}`}>
                {current.active_vm_count} / {currentCapLabel}
              </span>
            </div>
            <p className="text-[11px] text-neutral-600 mt-1">Counts PROVISIONING and IN_PROGRESS tasks.</p>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs text-neutral-300">Recent spend</span>
              <span className={`text-xs font-mono ${spendOverCeiling ? 'text-red-400' : 'text-neutral-100'}`}>
                {formatUsd(current.recent_spend_usd)} / {currentCeilingLabel}
              </span>
            </div>
            <p className="text-[11px] text-neutral-600 mt-1">{current.spend_window_days}-day rolling window.</p>
          </div>
        </div>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Edit Limits</h3>
        <p className="text-[11px] text-neutral-600 mb-3">
          Blank concurrent-VM or spend-ceiling fields clear the cap. Spend window stays required.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
          <label className="block">
            <span className="block text-xs text-neutral-400 mb-1">Max concurrent VMs</span>
            <input
              type="number"
              min={1}
              inputMode="numeric"
              value={maxConcurrentVmsInput}
              disabled={inputDisabled}
              onChange={e => setMaxConcurrentVmsInput(e.target.value)}
              placeholder="Unlimited"
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 disabled:opacity-60"
            />
          </label>
          <label className="block">
            <span className="block text-xs text-neutral-400 mb-1">Spend ceiling (USD)</span>
            <input
              type="number"
              min={0}
              step="0.01"
              inputMode="decimal"
              value={spendCeilingUsdInput}
              disabled={inputDisabled}
              onChange={e => setSpendCeilingUsdInput(e.target.value)}
              placeholder="Unlimited"
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 disabled:opacity-60"
            />
          </label>
          <label className="block">
            <span className="block text-xs text-neutral-400 mb-1">Spend window (days)</span>
            <input
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={spendWindowDaysInput}
              disabled={inputDisabled}
              onChange={e => setSpendWindowDaysInput(e.target.value)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 disabled:opacity-60"
            />
          </label>
          <label className="block">
            <span className="block text-xs text-neutral-400 mb-1">Max concurrent compute jobs</span>
            <input
              type="number"
              min={1}
              inputMode="numeric"
              value={maxConcurrentComputeJobsInput}
              disabled={inputDisabled}
              onChange={e => setMaxConcurrentComputeJobsInput(e.target.value)}
              placeholder="Unlimited"
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 disabled:opacity-60"
            />
          </label>
        </div>
        {error && <p className="mt-2 text-[11px] text-red-400">{error}</p>}
        {loading && <p className="mt-2 text-[11px] text-neutral-500">Loading resource envelope…</p>}
        {!canSave && <p className="mt-2 text-[11px] text-neutral-500">Only project owners can edit resource envelopes.</p>}
      </div>

      <div className="flex justify-end pt-2">
        <button
          onClick={handleSave}
          disabled={!canSubmit}
          className="inline-flex items-center justify-center min-h-[44px] bg-neutral-700 hover:bg-neutral-600 text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save Resource Envelope'}
        </button>
      </div>
    </div>
  );
}
