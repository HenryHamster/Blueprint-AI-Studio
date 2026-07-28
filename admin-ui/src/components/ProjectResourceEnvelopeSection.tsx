/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useMemo, useState } from 'react';
import type { ProjectResourceEnvelopeResponse } from '../types/project';
import type { ResourceEnvelopePatch } from '../api';
import { ConfirmDialog } from './ConfirmDialog';

interface ProjectResourceEnvelopeSectionProps {
  githubOwner: string | null;
  githubRepo: string | null;
  data: ProjectResourceEnvelopeResponse | null;
  loading: boolean;
  error: string | null;
  canSave: boolean;
  onSave: (patch: ResourceEnvelopePatch) => Promise<void>;
}

// Known hardware-policy values. These MUST mirror the backend's
// `supported_machine_types()` (vm_cost_rates.VM_COST_RATES) and
// `SUPPORTED_ACCELERATORS` — the backend rejects any unknown item, so the UI
// only ever offers values from these sets.
const KNOWN_MACHINE_TYPES = [
  'e2-standard-2',
  'e2-standard-4',
  'e2-standard-8',
  'e2-standard-16',
  'e2-standard-32',
] as const;
const KNOWN_ACCELERATOR_TYPES = ['T4', 'L4', 'V100'] as const;
type ExecutionPlacementPolicy = 'local_only' | 'local_preferred' | 'managed_allowed';
type ExecutionPlacementSelection = ExecutionPlacementPolicy | 'legacy_compute_only';

function formatUsd(value: number | null | undefined): string {
  return value == null ? 'Unlimited' : `$${value.toFixed(2)}`;
}

function formatCount(value: number | null | undefined): string {
  return value == null ? 'Unlimited' : String(value);
}

/** Machine-type policy display: null/undefined = unlimited, else the list. */
function formatMachinePolicy(value: string[] | null | undefined): string {
  if (value == null) return 'Unlimited';
  if (value.length === 0) return 'None allowed';
  return value.join(', ');
}

/** Accelerator policy display: null/undefined = unlimited, [] = all GPUs blocked. */
function formatAcceleratorPolicy(value: string[] | null | undefined): string {
  if (value == null) return 'Unlimited';
  if (value.length === 0) return 'All GPU jobs blocked';
  return value.join(', ');
}

/** Order-insensitive equality for `string[] | null` policy values. A `null`
 *  policy (unrestricted) is distinct from an empty list (explicit block). */
function policyEquals(a: string[] | null, b: string[] | null): boolean {
  if (a == null || b == null) return a === b;
  if (a.length !== b.length) return false;
  const sortedA = [...a].sort();
  const sortedB = [...b].sort();
  return sortedA.every((item, i) => item === sortedB[i]);
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
  const [spendWarningPctInput, setSpendWarningPctInput] = useState('');
  const [maxConcurrentComputeJobsInput, setMaxConcurrentComputeJobsInput] = useState('');
  const [machineTypes, setMachineTypes] = useState<string[]>([]);
  const [acceleratorTypes, setAcceleratorTypes] = useState<string[]>([]);
  // Distinct from "no accelerators selected": when true we save an explicit
  // empty list (`[]`) that blocks all GPU jobs, rather than `null` (unlimited).
  const [blockAllGpu, setBlockAllGpu] = useState(false);
  const [executionPlacementPolicy, setExecutionPlacementPolicy] =
    useState<ExecutionPlacementSelection>('managed_allowed');
  const [pauseOnCeilingExceeded, setPauseOnCeilingExceeded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pendingPlacementSave, setPendingPlacementSave] = useState<{
    patch: ResourceEnvelopePatch;
    policy: 'local_preferred' | 'managed_allowed';
  } | null>(null);

  useEffect(() => {
    setMaxConcurrentVmsInput(data?.max_concurrent_vms != null ? String(data.max_concurrent_vms) : '');
    setSpendCeilingUsdInput(data?.spend_ceiling_usd != null ? String(data.spend_ceiling_usd) : '');
    setSpendWindowDaysInput(String(data?.spend_window_days ?? 30));
    setSpendWarningPctInput(data?.spend_warning_fraction != null ? String(Math.round(data.spend_warning_fraction * 100)) : '');
    setMaxConcurrentComputeJobsInput(data?.max_concurrent_compute_jobs != null ? String(data.max_concurrent_compute_jobs) : '');
    setMachineTypes(data?.allowed_machine_types != null ? [...data.allowed_machine_types] : []);
    const accel = data?.allowed_accelerator_types;
    // An explicit empty list means "block all GPUs"; a populated list is a
    // whitelist; null/absent means unrestricted.
    setBlockAllGpu(Array.isArray(accel) && accel.length === 0);
    setAcceleratorTypes(accel != null ? [...accel] : []);
    // Preserve the legacy flag's deliberately narrow scope.  Showing it as a
    // sentinel forces an owner to choose a broad replacement policy explicitly;
    // simply opening or saving this form cannot broaden the legacy setting.
    const legacyPolicyActive = data?.legacy_require_local_runner_active
      ?? (data?.require_local_runner === true && !data?.execution_placement_policy_is_explicit);
    setExecutionPlacementPolicy(
      legacyPolicyActive
        ? 'legacy_compute_only'
        : (data?.execution_placement_policy ?? 'managed_allowed'),
    );
    setPauseOnCeilingExceeded(data?.pause_on_ceiling_exceeded ?? false);
    setPendingPlacementSave(null);
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

  const parsedSpendWarningFraction = useMemo(() => {
    if (!spendWarningPctInput.trim()) return null;
    const parsed = Number(spendWarningPctInput);
    return Number.isFinite(parsed) && parsed > 0 && parsed <= 100 ? parsed / 100 : Number.NaN;
  }, [spendWarningPctInput]);

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
    spend_warning_fraction: null,
    max_concurrent_compute_jobs: null,
    allowed_machine_types: null,
    allowed_accelerator_types: null,
    execution_placement_policy: 'managed_allowed' as ExecutionPlacementPolicy,
    execution_placement_policy_is_explicit: false,
    legacy_require_local_runner_active: false,
    require_local_runner: false,
    pause_on_ceiling_exceeded: false,
    active_vm_count: 0,
    recent_spend_usd: 0,
    recent_spend_compute_usd: 0,
    recent_spend_agent_vm_usd: 0,
    recent_spend_idle_usd: 0,
    is_throttled: false,
  };

  const displayLoading = data == null && loading;
  const displayUnavailable = data == null && !!error;
  const noDataLabel = displayLoading ? 'Loading…' : displayUnavailable ? 'Unavailable' : null;

  const currentCapLabel = formatCount(current.max_concurrent_vms);
  const currentCeilingLabel = formatUsd(current.spend_ceiling_usd);
  const activeOverCap =
    current.max_concurrent_vms != null && current.active_vm_count > current.max_concurrent_vms;
  const spendOverCeiling =
    current.spend_ceiling_usd != null && current.recent_spend_usd > current.spend_ceiling_usd;

  // Resolve the hardware-policy values that would be saved. A fully-unchecked
  // machine-type set clears the policy (null). For accelerators, the explicit
  // "block all GPU jobs" toggle wins and saves an empty list; otherwise a
  // fully-unchecked set clears the policy (null).
  const machineTypesToSave = useMemo<string[] | null>(
    () => (machineTypes.length > 0 ? KNOWN_MACHINE_TYPES.filter(m => machineTypes.includes(m)) : null),
    [machineTypes],
  );
  const acceleratorTypesToSave = useMemo<string[] | null>(() => {
    if (blockAllGpu) return [];
    return acceleratorTypes.length > 0 ? KNOWN_ACCELERATOR_TYPES.filter(a => acceleratorTypes.includes(a)) : null;
  }, [acceleratorTypes, blockAllGpu]);

  const hasChanges = useMemo(() => {
    const maxChanged = maxConcurrentVmsInput.trim()
      ? String(current.max_concurrent_vms ?? '') !== maxConcurrentVmsInput.trim()
      : current.max_concurrent_vms !== null;
    const spendChanged = spendCeilingUsdInput.trim()
      ? String(current.spend_ceiling_usd ?? '') !== spendCeilingUsdInput.trim()
      : current.spend_ceiling_usd !== null;
    const windowChanged = String(current.spend_window_days ?? 30) !== spendWindowDaysInput.trim();
    const warningChanged = spendWarningPctInput.trim()
      ? String(Math.round((current.spend_warning_fraction ?? 0.8) * 100)) !== spendWarningPctInput.trim()
      : current.spend_warning_fraction != null;
    const computeJobsChanged = maxConcurrentComputeJobsInput.trim()
      ? String(current.max_concurrent_compute_jobs ?? '') !== maxConcurrentComputeJobsInput.trim()
      : current.max_concurrent_compute_jobs !== null;
    const machineTypesChanged = !policyEquals(current.allowed_machine_types ?? null, machineTypesToSave);
    const acceleratorTypesChanged = !policyEquals(current.allowed_accelerator_types ?? null, acceleratorTypesToSave);
    const legacyPolicyActive = current.legacy_require_local_runner_active
      ?? (current.require_local_runner === true && !current.execution_placement_policy_is_explicit);
    const currentPlacementPolicy = current.execution_placement_policy ?? 'managed_allowed';
    const executionPlacementPolicyChanged = legacyPolicyActive
      ? executionPlacementPolicy !== 'legacy_compute_only'
      : executionPlacementPolicy !== currentPlacementPolicy;
    const pauseOnCeilingExceededChanged = (current.pause_on_ceiling_exceeded ?? false) !== pauseOnCeilingExceeded;
    return (
      maxChanged ||
      spendChanged ||
      windowChanged ||
      warningChanged ||
      computeJobsChanged ||
      machineTypesChanged ||
      acceleratorTypesChanged ||
      executionPlacementPolicyChanged ||
      pauseOnCeilingExceededChanged
    );
  }, [
    current.max_concurrent_vms,
    current.spend_ceiling_usd,
    current.spend_window_days,
    current.spend_warning_fraction,
    current.max_concurrent_compute_jobs,
    current.allowed_machine_types,
    current.allowed_accelerator_types,
    current.execution_placement_policy,
    current.execution_placement_policy_is_explicit,
    current.legacy_require_local_runner_active,
    current.require_local_runner,
    current.pause_on_ceiling_exceeded,
    maxConcurrentVmsInput,
    spendCeilingUsdInput,
    spendWindowDaysInput,
    spendWarningPctInput,
    maxConcurrentComputeJobsInput,
    machineTypesToSave,
    acceleratorTypesToSave,
    executionPlacementPolicy,
    pauseOnCeilingExceeded,
  ]);

  const savePatch = async (patch: ResourceEnvelopePatch) => {
    setSaving(true);
    try {
      await onSave(patch);
    } finally {
      setSaving(false);
      setPendingPlacementSave(null);
    }
  };

  const handleSave = () => {
    if (!canSave || Number.isNaN(parsedMaxConcurrent) || Number.isNaN(parsedSpendCeiling) || Number.isNaN(parsedSpendWindowDays) || Number.isNaN(parsedSpendWarningFraction) || Number.isNaN(parsedMaxConcurrentComputeJobs)) {
      return;
    }

    const patch: ResourceEnvelopePatch = {
      max_concurrent_vms: parsedMaxConcurrent,
      spend_ceiling_usd: parsedSpendCeiling,
      spend_window_days: parsedSpendWindowDays,
      spend_warning_fraction: parsedSpendWarningFraction,
      max_concurrent_compute_jobs: parsedMaxConcurrentComputeJobs,
      allowed_machine_types: machineTypesToSave,
      allowed_accelerator_types: acceleratorTypesToSave,
      pause_on_ceiling_exceeded: pauseOnCeilingExceeded,
    };
    const legacyPolicyActive = current.legacy_require_local_runner_active
      ?? (current.require_local_runner === true && !current.execution_placement_policy_is_explicit);
    const currentPlacementPolicy = current.execution_placement_policy ?? 'managed_allowed';
    const executionPlacementPolicyChanged = legacyPolicyActive
      ? executionPlacementPolicy !== 'legacy_compute_only'
      : executionPlacementPolicy !== currentPlacementPolicy;
    if (executionPlacementPolicyChanged && executionPlacementPolicy !== 'legacy_compute_only') {
      patch.execution_placement_policy = executionPlacementPolicy;
      // An explicit replacement must clear the old field in the same atomic
      // PATCH. The backend rejects a broad policy plus legacy true.
      if (legacyPolicyActive) patch.require_local_runner = null;
    }

    // A project that was strict (including the old compute-experiment-only
    // contract) is about to permit an immediate managed fallback. Confirm the
    // exact patch rather than saving a broader policy as a side effect of other
    // resource-envelope edits.
    const placementBroadensManagedExecution =
      (legacyPolicyActive || currentPlacementPolicy === 'local_only')
      && (executionPlacementPolicy === 'local_preferred' || executionPlacementPolicy === 'managed_allowed');
    if (placementBroadensManagedExecution) {
      setPendingPlacementSave({ patch, policy: executionPlacementPolicy });
      return;
    }
    void savePatch(patch);
  };

  const toggleMachineType = (type: string) => {
    setMachineTypes(prev => (prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type]));
  };
  const toggleAcceleratorType = (type: string) => {
    // Selecting a specific accelerator implies GPUs are not fully blocked.
    setBlockAllGpu(false);
    setAcceleratorTypes(prev => (prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type]));
  };
  const toggleBlockAllGpu = () => {
    setBlockAllGpu(prev => {
      const next = !prev;
      // Blocking all GPUs clears any specific whitelist selection.
      if (next) setAcceleratorTypes([]);
      return next;
    });
  };

  const inputDisabled = !canSave || saving;
  const canSubmit = canSave && hasChanges && !Number.isNaN(parsedMaxConcurrent) && !Number.isNaN(parsedSpendCeiling) && !Number.isNaN(parsedSpendWindowDays) && !Number.isNaN(parsedSpendWarningFraction) && !Number.isNaN(parsedMaxConcurrentComputeJobs);

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
        <div className="grid grid-cols-1 sm:grid-cols-5 gap-3">
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-ui-sm uppercase tracking-wider text-neutral-500">Concurrent VMs</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{noDataLabel ?? currentCapLabel}</div>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-ui-sm uppercase tracking-wider text-neutral-500">Spend ceiling</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{noDataLabel ?? currentCeilingLabel}</div>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-ui-sm uppercase tracking-wider text-neutral-500">Spend window</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{noDataLabel ?? `${current.spend_window_days} days`}</div>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-ui-sm uppercase tracking-wider text-neutral-500">Warning threshold</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{noDataLabel ?? `${Math.round((current.spend_warning_fraction ?? 0.8) * 100)}%`}</div>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-ui-sm uppercase tracking-wider text-neutral-500">Concurrent compute jobs</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100">{noDataLabel ?? formatCount(current.max_concurrent_compute_jobs)}</div>
          </div>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-ui-sm uppercase tracking-wider text-neutral-500">Machine types</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100 break-words">
              {noDataLabel ?? formatMachinePolicy(current.allowed_machine_types)}
            </div>
          </div>
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-3">
            <div className="text-ui-sm uppercase tracking-wider text-neutral-500">GPU accelerators</div>
            <div className="mt-1 text-sm font-semibold text-neutral-100 break-words">
              {noDataLabel ?? formatAcceleratorPolicy(current.allowed_accelerator_types)}
            </div>
          </div>
        </div>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Live Usage</h3>
        <div className="space-y-3">
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs text-neutral-300">Active tasks</span>
              <span className={`text-xs font-mono ${!noDataLabel && activeOverCap ? 'text-red-400' : 'text-neutral-100'}`}>
                {noDataLabel ?? `${current.active_vm_count} / ${currentCapLabel}`}
              </span>
            </div>
            <p className="text-ui-md text-neutral-600 mt-1">Counts PROVISIONING and IN_PROGRESS tasks.</p>
          </div>
          {!noDataLabel && current.is_throttled && (
            <div className="bg-amber-950/50 border border-amber-600 rounded-lg px-4 py-2 flex items-center gap-2">
              <span className="text-amber-400" aria-hidden="true">⚠</span>
              <span className="text-xs font-semibold text-amber-300">
                Compute throttled — spend ceiling reached
              </span>
            </div>
          )}
          <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3">
            <div className="text-xs text-neutral-300 mb-2">Recent spend</div>
            {noDataLabel ? (
              <div className="text-xs font-mono text-neutral-100">{noDataLabel}</div>
            ) : (
              <div className="space-y-1">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-ui-md text-neutral-400">Compute jobs</span>
                  <span className="text-xs font-mono text-neutral-100">{formatUsd(current.recent_spend_compute_usd)}</span>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-ui-md text-neutral-400">Agent VMs</span>
                  <span className="text-xs font-mono text-neutral-100">{formatUsd(current.recent_spend_agent_vm_usd)}</span>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className="text-ui-md text-neutral-400">Warm-pool idle</span>
                  <span className="text-xs font-mono text-neutral-100">{formatUsd(current.recent_spend_idle_usd)}</span>
                </div>
                <div className="flex items-center justify-between gap-3 border-t border-neutral-800 pt-1 mt-1">
                  <span className="text-ui-md font-semibold text-neutral-300">Total</span>
                  <span className={`text-xs font-mono ${spendOverCeiling ? 'text-red-400' : 'text-neutral-100'}`}>
                    {`${formatUsd(current.recent_spend_usd)} / ${currentCeilingLabel}`}
                  </span>
                </div>
              </div>
            )}
            <p className="text-ui-md text-neutral-600 mt-2">{current.spend_window_days}-day rolling window.</p>
          </div>
        </div>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Edit Limits</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          Blank concurrent-VM or spend-ceiling fields clear the cap. Spend window stays required.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-5 gap-3">
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
            <span className="block text-xs text-neutral-400 mb-1">Spend warning threshold (%)</span>
            <input
              type="number"
              min={1}
              max={100}
              step={1}
              inputMode="numeric"
              value={spendWarningPctInput}
              disabled={inputDisabled}
              onChange={e => setSpendWarningPctInput(e.target.value)}
              placeholder="80 (default)"
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
        {error && <p className="mt-2 text-ui-md text-red-400">{error}</p>}
        {loading && <p className="mt-2 text-ui-md text-neutral-500">Loading resource envelope…</p>}
        {!canSave && <p className="mt-2 text-ui-md text-neutral-500">Only project owners can edit resource envelopes.</p>}
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Hardware Policy</h3>
        <p className="text-ui-md text-neutral-600 mb-3">
          Leave all boxes unchecked to allow any hardware (no restriction). Checking a subset
          whitelists only those values for this project's compute jobs.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <fieldset>
            <legend className="block text-xs text-neutral-400 mb-2">Allowed CPU machine types</legend>
            <div className="space-y-1">
              {KNOWN_MACHINE_TYPES.map(type => (
                <label key={type} className="flex items-center gap-2 min-h-[44px] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={machineTypes.includes(type)}
                    disabled={inputDisabled}
                    onChange={() => toggleMachineType(type)}
                    className="h-4 w-4 rounded border-neutral-700 bg-neutral-950 disabled:opacity-60"
                  />
                  <span className="text-xs font-mono text-neutral-200">{type}</span>
                </label>
              ))}
            </div>
            <p className="text-ui-md text-neutral-600 mt-1">
              {machineTypesToSave == null ? 'Any (unlimited)' : `Whitelist: ${machineTypesToSave.join(', ')}`}
            </p>
          </fieldset>
          <fieldset>
            <legend className="block text-xs text-neutral-400 mb-2">Allowed GPU accelerator types</legend>
            <div className="space-y-1">
              {KNOWN_ACCELERATOR_TYPES.map(type => (
                <label key={type} className="flex items-center gap-2 min-h-[44px] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={acceleratorTypes.includes(type)}
                    disabled={inputDisabled || blockAllGpu}
                    onChange={() => toggleAcceleratorType(type)}
                    className="h-4 w-4 rounded border-neutral-700 bg-neutral-950 disabled:opacity-60"
                  />
                  <span className="text-xs font-mono text-neutral-200">{type}</span>
                </label>
              ))}
              <label className="flex items-center gap-2 min-h-[44px] cursor-pointer border-t border-neutral-800 mt-1 pt-1">
                <input
                  type="checkbox"
                  checked={blockAllGpu}
                  disabled={inputDisabled}
                  onChange={toggleBlockAllGpu}
                  className="h-4 w-4 rounded border-neutral-700 bg-neutral-950 disabled:opacity-60"
                />
                <span className="text-xs text-amber-300">Block all GPU jobs</span>
              </label>
            </div>
            <p className="text-ui-md text-neutral-600 mt-1">
              {acceleratorTypesToSave == null
                ? 'Any (unlimited)'
                : acceleratorTypesToSave.length === 0
                  ? 'All GPU jobs blocked'
                  : `Whitelist: ${acceleratorTypesToSave.join(', ')}`}
            </p>
          </fieldset>
        </div>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Execution Placement</h3>
        <label className="block">
          <span className="block text-xs text-neutral-400 mb-1">Where executable work may run</span>
          <select
            aria-label="Execution placement policy"
            value={executionPlacementPolicy}
            disabled={inputDisabled}
            onChange={e => setExecutionPlacementPolicy(e.target.value as ExecutionPlacementSelection)}
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 disabled:opacity-60"
          >
            {executionPlacementPolicy === 'legacy_compute_only' && (
              <option value="legacy_compute_only" disabled>Legacy: local only for compute experiments</option>
            )}
            <option value="local_only">Local only — never use managed execution</option>
            <option value="local_preferred">Local preferred — try local first, then immediately fall back</option>
            <option value="managed_allowed">Managed allowed — same immediate fallback behavior today</option>
          </select>
        </label>
        {(current.legacy_require_local_runner_active
          ?? (current.require_local_runner === true && !current.execution_placement_policy_is_explicit)) && (
          <p className="text-ui-md text-amber-300 mt-2">
            Legacy policy active: only compute experiments require local runners. Select a new
            placement policy and save to replace it deliberately; it is not automatically broadened.
          </p>
        )}
        <p className="text-ui-md text-neutral-600 mt-1">
          This policy applies across supported executable work. Local-only work waits indefinitely
          for local capacity instead of silently creating billable managed execution. Today,
          local-preferred tries an eligible local runner first; if none is eligible in that dispatch
          cycle, it immediately continues to configured warm-pool or managed execution — there is no
          local-capacity timeout. Managed-allowed has the same fallback behavior today.
        </p>
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Spend Ceiling Action</h3>
        <label className="flex items-start gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={pauseOnCeilingExceeded}
            disabled={inputDisabled}
            onChange={() => setPauseOnCeilingExceeded(prev => !prev)}
            className="h-4 w-4 mt-0.5 rounded border-neutral-700 bg-neutral-950 disabled:opacity-60"
          />
          <span className="text-xs text-neutral-200">
            Pause project when spend ceiling is exceeded
            <span className="block text-ui-md text-neutral-600 mt-1">
              When on, exceeding the spend ceiling automatically pauses the project until
              an operator manually unpauses it. No new task will dispatch while paused.
              When off (the default), tasks are soft-deferred with a status message but
              the project stays running.
            </span>
          </span>
        </label>
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

      {pendingPlacementSave && (
        <ConfirmDialog
          title="Allow managed execution fallback?"
          message={pendingPlacementSave.policy === 'local_preferred'
            ? 'This changes a strict local policy to local-preferred. Under the current dispatcher, an eligible local runner is tried first, but if none is eligible in that dispatch cycle the task immediately continues to configured warm-pool or managed execution. There is no local-capacity timeout.'
            : 'This changes a strict local policy to managed-allowed. Under the current dispatcher, an eligible local runner is tried first, but if none is eligible in that dispatch cycle the task immediately continues to configured warm-pool or managed execution.'}
          confirmLabel="Allow managed fallback"
          onConfirm={() => void savePatch(pendingPlacementSave.patch)}
          onCancel={() => setPendingPlacementSave(null)}
        />
      )}
    </div>
  );
}
