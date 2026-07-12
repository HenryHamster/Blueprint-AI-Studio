/* eslint-disable react-hooks/set-state-in-effect */
import { useCallback, useEffect, useState } from 'react';
import { Loader2, RotateCcw } from 'lucide-react';
import { useTaskCapabilities } from '../taskCapabilities';
import {
  fetchEvergreenFillSettings,
  patchEvergreenFillSettings,
  patchStructuralHealthSettings,
  patchMetricsIntegritySettings,
  patchCapabilityProofGateSettings,
  apiFetch,
  type EvergreenFillSettings,
  type CapabilityProofGateMode,
} from '../api';
import { EvergreenFillObservabilityPanel } from './EvergreenFillObservabilityPanel';
import { StructuralHealthObservabilityPanel } from './StructuralHealthObservabilityPanel';
import { MetricsIntegrityObservabilityPanel } from './MetricsIntegrityObservabilityPanel';
import { EvergreenBacklogPanel } from './EvergreenBacklogPanel';

interface SettingsOrchestratorSectionProps {
  maxConcurrentVMs: number;
  onUpdateMaxVMs: (value: number) => void;
  maxRetries: number;
  onUpdateMaxRetries: (value: number) => void;
  requireApprovalGates: boolean;
  onUpdateRequireApprovalGates: (value: boolean) => void;
  autoArchiveEnabled: boolean;
  onUpdateAutoArchiveEnabled: (value: boolean) => void;
  autoArchiveTerminalAfterDays: number;
  onUpdateAutoArchiveTerminalAfterDays: (value: number) => void;
  autoArchiveMaxTerminalTasksPerProject: number;
  onUpdateAutoArchiveMaxTerminalTasksPerProject: (value: number) => void;
  autoArchiveIntervalMinutes: number;
  onUpdateAutoArchiveIntervalMinutes: (value: number) => void;
  maxAutonomousTasksPerHour: number;
  onUpdateMaxAutonomousRate: (value: number) => void;
  maxGlobalAutonomousTasksPerHour: number;
  onUpdateMaxGlobalAutonomousRate: (value: number) => void;
  maxPipelineSlots: number;
  onUpdateMaxPipelineSlots: (value: number) => void;
  reservedSlotsForTypes: Record<string, number>;
  onUpdateReservedSlotsForTypes: (value: Record<string, number>) => void;
  evaluatingPipelineTimeoutMinutes: number;
  onUpdateEvaluatingPipelineTimeout: (value: number) => void;
  evaluatingFailHours: number;
  onUpdateEvaluatingFailHours: (value: number) => void;
  ciBackstopTimeoutMinutes: number;
  onUpdateCiBackstopTimeout: (value: number) => void;
  autonomousResearchIntervalMinutes: number;
  onUpdateAutonomousResearchInterval: (value: number) => void;
  directionCooldownMinutes: number;
  onUpdateDirectionCooldownMinutes: (value: number) => void;
  strategicResearchFrequency: number;
  onUpdateStrategicResearchFrequency: (value: number) => void;
  anyAutonomousEnabled: boolean;
  onPauseAllAutonomous: () => Promise<void>;
  onRestartOrchestrator: () => Promise<void>;
  selectedProjectId?: string | null;
  // Direction guardrail knobs
  directionChurnWindowCycles: number;
  onUpdateDirectionChurnWindowCycles: (value: number) => void;
  directionChurnNonMergeThreshold: number;
  onUpdateDirectionChurnNonMergeThreshold: (value: number) => void;
  directionChurnMinMergeRate: number;
  onUpdateDirectionChurnMinMergeRate: (value: number) => void;
  directionLifetimeNonMergeThreshold: number;
  onUpdateDirectionLifetimeNonMergeThreshold: (value: number) => void;
  directionLifetimeMinMergeRate: number;
  onUpdateDirectionLifetimeMinMergeRate: (value: number) => void;
  directionReviewRecommendationThreshold: number;
  onUpdateDirectionReviewRecommendationThreshold: (value: number) => void;
}

function Toggle({ enabled, onChange }: { enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      data-viewport-allow-small-target
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ${enabled ? 'bg-emerald-600' : 'bg-neutral-700'}`}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  );
}

export function SettingsOrchestratorSection({
  maxConcurrentVMs,
  onUpdateMaxVMs,
  maxRetries,
  onUpdateMaxRetries,
  requireApprovalGates,
  onUpdateRequireApprovalGates,
  autoArchiveEnabled,
  onUpdateAutoArchiveEnabled,
  autoArchiveTerminalAfterDays,
  onUpdateAutoArchiveTerminalAfterDays,
  autoArchiveMaxTerminalTasksPerProject,
  onUpdateAutoArchiveMaxTerminalTasksPerProject,
  autoArchiveIntervalMinutes,
  onUpdateAutoArchiveIntervalMinutes,
  maxAutonomousTasksPerHour,
  onUpdateMaxAutonomousRate,
  maxGlobalAutonomousTasksPerHour,
  onUpdateMaxGlobalAutonomousRate,
  maxPipelineSlots,
  onUpdateMaxPipelineSlots,
  reservedSlotsForTypes,
  onUpdateReservedSlotsForTypes,
  evaluatingPipelineTimeoutMinutes,
  onUpdateEvaluatingPipelineTimeout,
  evaluatingFailHours,
  onUpdateEvaluatingFailHours,
  ciBackstopTimeoutMinutes,
  onUpdateCiBackstopTimeout,
  autonomousResearchIntervalMinutes,
  onUpdateAutonomousResearchInterval,
  directionCooldownMinutes,
  onUpdateDirectionCooldownMinutes,
  strategicResearchFrequency,
  onUpdateStrategicResearchFrequency,
  anyAutonomousEnabled,
  onPauseAllAutonomous,
  onRestartOrchestrator,
  selectedProjectId = null,
  directionChurnWindowCycles,
  onUpdateDirectionChurnWindowCycles,
  directionChurnNonMergeThreshold,
  onUpdateDirectionChurnNonMergeThreshold,
  directionChurnMinMergeRate,
  onUpdateDirectionChurnMinMergeRate,
  directionLifetimeNonMergeThreshold,
  onUpdateDirectionLifetimeNonMergeThreshold,
  directionLifetimeMinMergeRate,
  onUpdateDirectionLifetimeMinMergeRate,
  directionReviewRecommendationThreshold,
  onUpdateDirectionReviewRecommendationThreshold,
}: SettingsOrchestratorSectionProps) {
  // Reservable task types derive from the published capability contract so a
  // newly registered reservable type gets a control here automatically.
  const { reservableIds } = useTaskCapabilities();
  const [vmInputVal, setVmInputVal] = useState(String(maxConcurrentVMs));
  const [retriesInputVal, setRetriesInputVal] = useState(String(maxRetries));
  const [autonomousRateInputVal, setAutonomousRateInputVal] = useState(String(maxAutonomousTasksPerHour));
  const [globalRateInputVal, setGlobalRateInputVal] = useState(String(maxGlobalAutonomousTasksPerHour));
  const [pipelineSlotsInputVal, setPipelineSlotsInputVal] = useState(String(maxPipelineSlots));
  const [reservedInputVals, setReservedInputVals] = useState<Record<string, string>>(() =>
    Object.fromEntries(reservableIds.map(t => [t, String(reservedSlotsForTypes[t] ?? 0)])),
  );
  const [evalTimeoutInputVal, setEvalTimeoutInputVal] = useState(String(evaluatingPipelineTimeoutMinutes));
  const [evalFailHoursInputVal, setEvalFailHoursInputVal] = useState(String(evaluatingFailHours));
  const [ciBackstopInputVal, setCiBackstopInputVal] = useState(String(ciBackstopTimeoutMinutes));
  const [archiveAgeCapsInputVal, setArchiveAgeCapsInputVal] = useState(String(autoArchiveTerminalAfterDays));
  const [archivePerProjectCapInputVal, setArchivePerProjectCapInputVal] = useState(String(autoArchiveMaxTerminalTasksPerProject));
  const [archiveIntervalInputVal, setArchiveIntervalInputVal] = useState(String(autoArchiveIntervalMinutes));
  const [autonomousResearchIntervalInputVal, setAutonomousResearchIntervalInputVal] = useState(String(autonomousResearchIntervalMinutes));
  const [directionCooldownInputVal, setDirectionCooldownInputVal] = useState(String(directionCooldownMinutes));
  const [strategicResearchFreqInputVal, setStrategicResearchFreqInputVal] = useState(String(strategicResearchFrequency));
  // Direction guardrail input vals
  const [churnWindowInputVal, setChurnWindowInputVal] = useState(String(directionChurnWindowCycles));
  const [churnNonMergeThresholdInputVal, setChurnNonMergeThresholdInputVal] = useState(String(directionChurnNonMergeThreshold));
  const [churnMinMergeRateInputVal, setChurnMinMergeRateInputVal] = useState(String(directionChurnMinMergeRate));
  const [lifetimeNonMergeThresholdInputVal, setLifetimeNonMergeThresholdInputVal] = useState(String(directionLifetimeNonMergeThreshold));
  const [lifetimeMinMergeRateInputVal, setLifetimeMinMergeRateInputVal] = useState(String(directionLifetimeMinMergeRate));
  const [reviewRecommendThresholdInputVal, setReviewRecommendThresholdInputVal] = useState(String(directionReviewRecommendationThreshold));
  const [restarting, setRestarting] = useState(false);
  const [restartStatus, setRestartStatus] = useState<'idle' | 'success' | 'error'>('idle');

  // Evergreen fill settings — fetched directly and patched on change.
  const [fillSettings, setFillSettings] = useState<EvergreenFillSettings>({
    evergreen_fill_mode: 'off',
    evergreen_fill_interval_minutes: 15,
    evergreen_fill_util_threshold: 0.5,
    evergreen_fill_min_free_slots: 2,
    evergreen_fill_max_active: 1,
    evergreen_fill_daily_cap: 3,
    evergreen_fill_deferred_memo_enrollment: false,
  });
  const [fillIntervalInput, setFillIntervalInput] = useState('15');
  const [fillUtilInput, setFillUtilInput] = useState('0.5');
  const [fillMinSlotsInput, setFillMinSlotsInput] = useState('2');
  const [fillMaxActiveInput, setFillMaxActiveInput] = useState('1');
  const [fillDailyCapInput, setFillDailyCapInput] = useState('3');

  // Structural health audit mode — fetched directly and patched on change.
  const [auditMode, setAuditMode] = useState<string>('off');

  // Metrics integrity sweep mode — fetched via settings and patched on change.
  const [sweepMode, setSweepMode] = useState<string>('off');

  // Capability proof gate — fetched via settings and patched on change. Defaults
  // mirror the backend GET fallbacks (off / disabled / 0.5 / 0.95) so an older
  // payload missing these keys does not crash the panel.
  const [proofGateMode, setProofGateMode] = useState<CapabilityProofGateMode>('off');
  const [proofAutoGradEnabled, setProofAutoGradEnabled] = useState(false);
  const [proofObserveThreshold, setProofObserveThreshold] = useState(0.5);
  const [proofEnforceThreshold, setProofEnforceThreshold] = useState(0.95);
  const [proofObserveThresholdInput, setProofObserveThresholdInput] = useState('0.5');
  const [proofEnforceThresholdInput, setProofEnforceThresholdInput] = useState('0.95');

  const loadFillSettings = useCallback(async () => {
    try {
      const s = await fetchEvergreenFillSettings();
      setFillSettings(s);
      setFillIntervalInput(String(s.evergreen_fill_interval_minutes));
      setFillUtilInput(String(s.evergreen_fill_util_threshold));
      setFillMinSlotsInput(String(s.evergreen_fill_min_free_slots));
      setFillMaxActiveInput(String(s.evergreen_fill_max_active));
      setFillDailyCapInput(String(s.evergreen_fill_daily_cap));
    } catch {
      // silently ignore — defaults remain
    }
  }, []);

  const loadAuditMode = useCallback(async () => {
    try {
      const res = await apiFetch('/api/admin/settings');
      if (res.ok) {
        const data = await res.json();
        const mode = data?.structural_health_audit_mode ?? 'off';
        setAuditMode(String(mode));
        const sweep = data?.metrics_integrity_sweep_mode ?? 'off';
        setSweepMode(String(sweep));
        // Capability proof gate — default to off/false/0.5/0.95 for older payloads.
        const gateMode = data?.capability_proof_gate_mode ?? 'off';
        setProofGateMode(
          gateMode === 'observe' || gateMode === 'enforce' ? gateMode : 'off',
        );
        setProofAutoGradEnabled(Boolean(data?.capability_proof_gate_auto_graduation_enabled ?? false));
        const observeT = Number(data?.capability_proof_gate_auto_graduation_observe_threshold ?? 0.5);
        const enforceT = Number(data?.capability_proof_gate_auto_graduation_enforce_threshold ?? 0.95);
        const safeObserve = Number.isFinite(observeT) ? observeT : 0.5;
        const safeEnforce = Number.isFinite(enforceT) ? enforceT : 0.95;
        setProofObserveThreshold(safeObserve);
        setProofEnforceThreshold(safeEnforce);
        setProofObserveThresholdInput(String(safeObserve));
        setProofEnforceThresholdInput(String(safeEnforce));
      }
    } catch {
      // silently ignore — defaults remain
    }
  }, []);

  useEffect(() => { void loadFillSettings(); }, [loadFillSettings]);
  useEffect(() => { void loadAuditMode(); }, [loadAuditMode]);

  const patchFill = useCallback(async (patch: Partial<EvergreenFillSettings>) => {
    try {
      await patchEvergreenFillSettings(patch);
      setFillSettings(s => ({ ...s, ...patch }));
    } catch {
      void loadFillSettings();
    }
  }, [loadFillSettings]);

  const patchAuditMode = useCallback(async (mode: string) => {
    try {
      await patchStructuralHealthSettings({ structural_health_audit_mode: mode });
      setAuditMode(mode);
    } catch {
      // on error, keep current mode
    }
  }, []);

  const patchSweepMode = useCallback(async (mode: string) => {
    try {
      await patchMetricsIntegritySettings({ metrics_integrity_sweep_mode: mode });
      setSweepMode(mode);
    } catch {
      // on error, keep current mode
    }
  }, []);

  const patchProofGateMode = useCallback(async (mode: CapabilityProofGateMode) => {
    try {
      await patchCapabilityProofGateSettings({ capability_proof_gate_mode: mode });
      setProofGateMode(mode);
    } catch {
      void loadAuditMode();
    }
  }, [loadAuditMode]);

  const patchProofAutoGrad = useCallback(async (enabled: boolean) => {
    try {
      await patchCapabilityProofGateSettings({ capability_proof_gate_auto_graduation_enabled: enabled });
      setProofAutoGradEnabled(enabled);
    } catch {
      void loadAuditMode();
    }
  }, [loadAuditMode]);

  // Commit a threshold input (observe/enforce). Only valid 0.0–1.0 values are
  // sent; anything else reverts the input to the last-applied value. The backend
  // additionally rejects out-of-range values, so this is a client-side hint.
  const commitProofThreshold = useCallback((
    key:
      | 'capability_proof_gate_auto_graduation_observe_threshold'
      | 'capability_proof_gate_auto_graduation_enforce_threshold',
    raw: string,
    fallback: number,
    applyValue: (v: number) => void,
    setInput: (v: string) => void,
  ) => {
    const n = parseFloat(raw);
    if (!isNaN(n) && n >= 0 && n <= 1) {
      applyValue(n);
      setInput(String(n));
      void (async () => {
        try {
          await patchCapabilityProofGateSettings({ [key]: n });
        } catch {
          void loadAuditMode();
        }
      })();
    } else {
      setInput(String(fallback));
    }
  }, [loadAuditMode]);

  const commitFillNumber = useCallback((
    key: keyof EvergreenFillSettings,
    raw: string,
    min: number,
    max: number,
    fallback: number,
    setter: (v: string) => void,
  ) => {
    const n = parseFloat(raw);
    if (!isNaN(n) && n >= min && n <= max) {
      void patchFill({ [key]: n });
    } else {
      setter(String(fallback));
    }
  }, [patchFill]);

  useEffect(() => { setVmInputVal(String(maxConcurrentVMs)); }, [maxConcurrentVMs]);
  useEffect(() => { setRetriesInputVal(String(maxRetries)); }, [maxRetries]);
  useEffect(() => { setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); }, [maxAutonomousTasksPerHour]);
  useEffect(() => { setGlobalRateInputVal(String(maxGlobalAutonomousTasksPerHour)); }, [maxGlobalAutonomousTasksPerHour]);
  useEffect(() => { setPipelineSlotsInputVal(String(maxPipelineSlots)); }, [maxPipelineSlots]);
  useEffect(() => {
    setReservedInputVals(Object.fromEntries(reservableIds.map(t => [t, String(reservedSlotsForTypes[t] ?? 0)])));
  }, [reservedSlotsForTypes, reservableIds]);
  useEffect(() => { setEvalTimeoutInputVal(String(evaluatingPipelineTimeoutMinutes)); }, [evaluatingPipelineTimeoutMinutes]);
  useEffect(() => { setEvalFailHoursInputVal(String(evaluatingFailHours)); }, [evaluatingFailHours]);
  useEffect(() => { setCiBackstopInputVal(String(ciBackstopTimeoutMinutes)); }, [ciBackstopTimeoutMinutes]);
  useEffect(() => { setArchiveAgeCapsInputVal(String(autoArchiveTerminalAfterDays)); }, [autoArchiveTerminalAfterDays]);
  useEffect(() => { setArchivePerProjectCapInputVal(String(autoArchiveMaxTerminalTasksPerProject)); }, [autoArchiveMaxTerminalTasksPerProject]);
  useEffect(() => { setArchiveIntervalInputVal(String(autoArchiveIntervalMinutes)); }, [autoArchiveIntervalMinutes]);
  useEffect(() => { setAutonomousResearchIntervalInputVal(String(autonomousResearchIntervalMinutes)); }, [autonomousResearchIntervalMinutes]);
  useEffect(() => { setDirectionCooldownInputVal(String(directionCooldownMinutes)); }, [directionCooldownMinutes]);
  useEffect(() => { setStrategicResearchFreqInputVal(String(strategicResearchFrequency)); }, [strategicResearchFrequency]);
  useEffect(() => { setChurnWindowInputVal(String(directionChurnWindowCycles)); }, [directionChurnWindowCycles]);
  useEffect(() => { setChurnNonMergeThresholdInputVal(String(directionChurnNonMergeThreshold)); }, [directionChurnNonMergeThreshold]);
  useEffect(() => { setChurnMinMergeRateInputVal(String(directionChurnMinMergeRate)); }, [directionChurnMinMergeRate]);
  useEffect(() => { setLifetimeNonMergeThresholdInputVal(String(directionLifetimeNonMergeThreshold)); }, [directionLifetimeNonMergeThreshold]);
  useEffect(() => { setLifetimeMinMergeRateInputVal(String(directionLifetimeMinMergeRate)); }, [directionLifetimeMinMergeRate]);
  useEffect(() => { setReviewRecommendThresholdInputVal(String(directionReviewRecommendationThreshold)); }, [directionReviewRecommendationThreshold]);

  const reserved = (t: string) => reservedSlotsForTypes[t] ?? 0;
  const setReserved = (t: string, value: number) =>
    onUpdateReservedSlotsForTypes({ ...reservedSlotsForTypes, [t]: Math.max(0, Math.min(10, value)) });
  const commitReserved = (t: string) => {
    const n = parseInt(reservedInputVals[t]);
    if (!isNaN(n) && n >= 0 && n <= 10) setReserved(t, n);
    else setReservedInputVals(v => ({ ...v, [t]: String(reserved(t)) }));
  };

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Pipeline Settings</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label htmlFor="orch-max-vms" className="block text-xs font-medium text-neutral-400 mb-1.5">Max Concurrent VMs (0-100)</label>
            <input
              id="orch-max-vms"
              type="number"
              min={0}
              max={100}
              value={vmInputVal}
              onChange={e => setVmInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(vmInputVal); if (n >= 0 && n <= 100) onUpdateMaxVMs(n); else setVmInputVal(String(maxConcurrentVMs)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(vmInputVal); if (n >= 0 && n <= 100) onUpdateMaxVMs(n); else setVmInputVal(String(maxConcurrentVMs)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="orch-max-retries" className="block text-xs font-medium text-neutral-400 mb-1.5">Max Retries (0-10)</label>
            <input
              id="orch-max-retries"
              type="number"
              min={0}
              max={10}
              value={retriesInputVal}
              onChange={e => setRetriesInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(retriesInputVal); if (!isNaN(n) && n >= 0 && n <= 10) onUpdateMaxRetries(n); else setRetriesInputVal(String(maxRetries)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(retriesInputVal); if (!isNaN(n) && n >= 0 && n <= 10) onUpdateMaxRetries(n); else setRetriesInputVal(String(maxRetries)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="orch-max-autonomous-rate" className="block text-xs font-medium text-neutral-400 mb-1.5">Max Autonomous Tasks/Hour (1-50)</label>
            <input
              id="orch-max-autonomous-rate"
              type="number"
              min={1}
              max={50}
              value={autonomousRateInputVal}
              onChange={e => setAutonomousRateInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(autonomousRateInputVal); if (n >= 1 && n <= 50) onUpdateMaxAutonomousRate(n); else setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(autonomousRateInputVal); if (n >= 1 && n <= 50) onUpdateMaxAutonomousRate(n); else setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="orch-max-global-autonomous-rate" className="block text-xs font-medium text-neutral-400 mb-1.5">Global Autonomous Tasks/Hour (1-500)</label>
            <input
              id="orch-max-global-autonomous-rate"
              type="number"
              min={1}
              max={500}
              value={globalRateInputVal}
              onChange={e => setGlobalRateInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(globalRateInputVal); if (n >= 1 && n <= 500) onUpdateMaxGlobalAutonomousRate(n); else setGlobalRateInputVal(String(maxGlobalAutonomousTasksPerHour)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(globalRateInputVal); if (n >= 1 && n <= 500) onUpdateMaxGlobalAutonomousRate(n); else setGlobalRateInputVal(String(maxGlobalAutonomousTasksPerHour)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div className="flex items-end">
            <div className="w-full">
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Require Approval Gates</label>
              <div className="flex items-center gap-3 bg-neutral-950 border border-neutral-700 rounded-lg px-3 py-2">
                <Toggle enabled={requireApprovalGates} onChange={onUpdateRequireApprovalGates} />
                <span className="text-xs text-neutral-400">{requireApprovalGates ? 'Enabled' : 'Disabled'}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Pipeline Slots */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Pipeline Slots</h3>
        <p className="text-xs text-neutral-500 mb-3">
          Budget review/fix tasks (0 = disabled) and reserve slots for specific task types (0-10 each).
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div>
            <label htmlFor="orch-max-pipeline-slots" className="block text-xs font-medium text-neutral-400 mb-1.5">Max Pipeline Slots (0-100)</label>
            <input
              id="orch-max-pipeline-slots"
              type="number"
              min={0}
              max={100}
              value={pipelineSlotsInputVal}
              onChange={e => setPipelineSlotsInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(pipelineSlotsInputVal); if (!isNaN(n) && n >= 0 && n <= 100) onUpdateMaxPipelineSlots(n); else setPipelineSlotsInputVal(String(maxPipelineSlots)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(pipelineSlotsInputVal); if (!isNaN(n) && n >= 0 && n <= 100) onUpdateMaxPipelineSlots(n); else setPipelineSlotsInputVal(String(maxPipelineSlots)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          {reservableIds.map(t => (
            <div key={t}>
              <label htmlFor={`orch-reserved-${t}`} className="block text-xs font-medium text-neutral-400 mb-1.5">Reserved: {t} (0-10)</label>
              <input
                id={`orch-reserved-${t}`}
                type="number"
                min={0}
                max={10}
                value={reservedInputVals[t] ?? '0'}
                onChange={e => setReservedInputVals(v => ({ ...v, [t]: e.target.value }))}
                onBlur={() => commitReserved(t)}
                onKeyDown={e => { if (e.key === 'Enter') commitReserved(t); }}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
          ))}
        </div>
      </div>

      {/* Pipeline Timeouts */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Pipeline Timeouts</h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label htmlFor="orch-evaluating-pipeline-timeout" className="block text-xs font-medium text-neutral-400 mb-1.5">Evaluating Pipeline Timeout (1-1440 min)</label>
            <input
              id="orch-evaluating-pipeline-timeout"
              type="number"
              min={1}
              max={1440}
              value={evalTimeoutInputVal}
              onChange={e => setEvalTimeoutInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(evalTimeoutInputVal); if (!isNaN(n) && n >= 1 && n <= 1440) onUpdateEvaluatingPipelineTimeout(n); else setEvalTimeoutInputVal(String(evaluatingPipelineTimeoutMinutes)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(evalTimeoutInputVal); if (!isNaN(n) && n >= 1 && n <= 1440) onUpdateEvaluatingPipelineTimeout(n); else setEvalTimeoutInputVal(String(evaluatingPipelineTimeoutMinutes)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="orch-evaluating-fail-hours" className="block text-xs font-medium text-neutral-400 mb-1.5">Evaluating Fail Hours (1-168)</label>
            <input
              id="orch-evaluating-fail-hours"
              type="number"
              min={1}
              max={168}
              value={evalFailHoursInputVal}
              onChange={e => setEvalFailHoursInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(evalFailHoursInputVal); if (!isNaN(n) && n >= 1 && n <= 168) onUpdateEvaluatingFailHours(n); else setEvalFailHoursInputVal(String(evaluatingFailHours)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(evalFailHoursInputVal); if (!isNaN(n) && n >= 1 && n <= 168) onUpdateEvaluatingFailHours(n); else setEvalFailHoursInputVal(String(evaluatingFailHours)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="orch-ci-backstop-timeout" className="block text-xs font-medium text-neutral-400 mb-1.5">CI Backstop Timeout (1-1440 min)</label>
            <input
              id="orch-ci-backstop-timeout"
              type="number"
              min={1}
              max={1440}
              value={ciBackstopInputVal}
              onChange={e => setCiBackstopInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(ciBackstopInputVal); if (!isNaN(n) && n >= 1 && n <= 1440) onUpdateCiBackstopTimeout(n); else setCiBackstopInputVal(String(ciBackstopTimeoutMinutes)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(ciBackstopInputVal); if (!isNaN(n) && n >= 1 && n <= 1440) onUpdateCiBackstopTimeout(n); else setCiBackstopInputVal(String(ciBackstopTimeoutMinutes)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
        </div>
      </div>

      {/* Auto-Archive */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Auto-Archive Terminal Tasks</h3>
        <p className="text-xs text-neutral-500 mb-3">
          Archives tasks when EITHER cap hits: age (days since terminal) or per-project count (keep most-recent N).
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Enabled</label>
            <div className="flex items-center gap-3 bg-neutral-950 border border-neutral-700 rounded-lg px-3 py-2">
              <Toggle enabled={autoArchiveEnabled} onChange={onUpdateAutoArchiveEnabled} />
              <span className="text-xs text-neutral-400">{autoArchiveEnabled ? 'On' : 'Off'}</span>
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Age Cap (1-90 days)</label>
            <input
              type="number"
              min={1}
              max={90}
              value={archiveAgeCapsInputVal}
              onChange={e => setArchiveAgeCapsInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(archiveAgeCapsInputVal); if (!isNaN(n) && n >= 1 && n <= 90) onUpdateAutoArchiveTerminalAfterDays(n); else setArchiveAgeCapsInputVal(String(autoArchiveTerminalAfterDays)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(archiveAgeCapsInputVal); if (!isNaN(n) && n >= 1 && n <= 90) onUpdateAutoArchiveTerminalAfterDays(n); else setArchiveAgeCapsInputVal(String(autoArchiveTerminalAfterDays)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Per-Project Cap (0=off)</label>
            <input
              type="number"
              min={0}
              max={10000}
              value={archivePerProjectCapInputVal}
              onChange={e => setArchivePerProjectCapInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(archivePerProjectCapInputVal); if (!isNaN(n) && n >= 0 && n <= 10000) onUpdateAutoArchiveMaxTerminalTasksPerProject(n); else setArchivePerProjectCapInputVal(String(autoArchiveMaxTerminalTasksPerProject)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(archivePerProjectCapInputVal); if (!isNaN(n) && n >= 0 && n <= 10000) onUpdateAutoArchiveMaxTerminalTasksPerProject(n); else setArchivePerProjectCapInputVal(String(autoArchiveMaxTerminalTasksPerProject)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Interval (5-1440 min)</label>
            <input
              type="number"
              min={5}
              max={1440}
              value={archiveIntervalInputVal}
              onChange={e => setArchiveIntervalInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(archiveIntervalInputVal); if (!isNaN(n) && n >= 5 && n <= 1440) onUpdateAutoArchiveIntervalMinutes(n); else setArchiveIntervalInputVal(String(autoArchiveIntervalMinutes)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(archiveIntervalInputVal); if (!isNaN(n) && n >= 5 && n <= 1440) onUpdateAutoArchiveIntervalMinutes(n); else setArchiveIntervalInputVal(String(autoArchiveIntervalMinutes)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
        </div>
      </div>

      {/* Autonomous Research */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Autonomous Research</h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Research Interval (min)</label>
            <input
              type="number"
              min={1}
              max={1440}
              value={autonomousResearchIntervalInputVal}
              onChange={e => setAutonomousResearchIntervalInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(autonomousResearchIntervalInputVal); if (!isNaN(n) && n >= 1 && n <= 1440) onUpdateAutonomousResearchInterval(n); else setAutonomousResearchIntervalInputVal(String(autonomousResearchIntervalMinutes)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(autonomousResearchIntervalInputVal); if (!isNaN(n) && n >= 1 && n <= 1440) onUpdateAutonomousResearchInterval(n); else setAutonomousResearchIntervalInputVal(String(autonomousResearchIntervalMinutes)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Direction Cooldown (min)</label>
            <input
              type="number"
              min={1}
              max={10080}
              value={directionCooldownInputVal}
              onChange={e => setDirectionCooldownInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(directionCooldownInputVal); if (!isNaN(n) && n >= 1 && n <= 10080) onUpdateDirectionCooldownMinutes(n); else setDirectionCooldownInputVal(String(directionCooldownMinutes)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(directionCooldownInputVal); if (!isNaN(n) && n >= 1 && n <= 10080) onUpdateDirectionCooldownMinutes(n); else setDirectionCooldownInputVal(String(directionCooldownMinutes)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Strategic Freq (0=off)</label>
            <input
              type="number"
              min={0}
              max={100}
              value={strategicResearchFreqInputVal}
              onChange={e => setStrategicResearchFreqInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(strategicResearchFreqInputVal); if (!isNaN(n) && n >= 0 && n <= 100) onUpdateStrategicResearchFrequency(n); else setStrategicResearchFreqInputVal(String(strategicResearchFrequency)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(strategicResearchFreqInputVal); if (!isNaN(n) && n >= 0 && n <= 100) onUpdateStrategicResearchFrequency(n); else setStrategicResearchFreqInputVal(String(strategicResearchFrequency)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <p className="text-ui-sm text-neutral-600 mt-1">Every Nth cycle reviews vision</p>
          </div>
        </div>
        <button
          onClick={onPauseAllAutonomous}
          disabled={!anyAutonomousEnabled}
          className="mt-3 bg-amber-950/30 hover:bg-amber-900/40 disabled:opacity-30 disabled:cursor-not-allowed text-amber-400 text-xs py-2 px-4 rounded-lg transition-colors border border-amber-900/50"
        >
          Pause All Autonomous
        </button>
      </div>

      {/* Direction Guardrails */}
      <div data-testid="direction-guardrails-section">
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Direction Guardrails</h3>
        <p className="text-xs text-neutral-500 mb-3">
          Thresholds for the churn and lifetime-burn pause guardrails. A direction accumulating non-merging tasks beyond these thresholds is paused; after the review-recommendation threshold is crossed, a flagged retirement recommendation is recorded for operator review.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Churn Window (cycles)</label>
            <input
              data-testid="direction-churn-window-cycles"
              type="number"
              min={1}
              max={100}
              value={churnWindowInputVal}
              onChange={e => setChurnWindowInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(churnWindowInputVal); if (!isNaN(n) && n >= 1 && n <= 100) onUpdateDirectionChurnWindowCycles(n); else setChurnWindowInputVal(String(directionChurnWindowCycles)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(churnWindowInputVal); if (!isNaN(n) && n >= 1 && n <= 100) onUpdateDirectionChurnWindowCycles(n); else setChurnWindowInputVal(String(directionChurnWindowCycles)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Churn Non-Merge Threshold</label>
            <input
              data-testid="direction-churn-non-merge-threshold"
              type="number"
              min={1}
              max={10000}
              value={churnNonMergeThresholdInputVal}
              onChange={e => setChurnNonMergeThresholdInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(churnNonMergeThresholdInputVal); if (!isNaN(n) && n >= 1 && n <= 10000) onUpdateDirectionChurnNonMergeThreshold(n); else setChurnNonMergeThresholdInputVal(String(directionChurnNonMergeThreshold)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(churnNonMergeThresholdInputVal); if (!isNaN(n) && n >= 1 && n <= 10000) onUpdateDirectionChurnNonMergeThreshold(n); else setChurnNonMergeThresholdInputVal(String(directionChurnNonMergeThreshold)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Churn Min Merge Rate (0–1)</label>
            <input
              data-testid="direction-churn-min-merge-rate"
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={churnMinMergeRateInputVal}
              onChange={e => setChurnMinMergeRateInputVal(e.target.value)}
              onBlur={() => { const n = parseFloat(churnMinMergeRateInputVal); if (!isNaN(n) && n >= 0 && n <= 1) onUpdateDirectionChurnMinMergeRate(n); else setChurnMinMergeRateInputVal(String(directionChurnMinMergeRate)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseFloat(churnMinMergeRateInputVal); if (!isNaN(n) && n >= 0 && n <= 1) onUpdateDirectionChurnMinMergeRate(n); else setChurnMinMergeRateInputVal(String(directionChurnMinMergeRate)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Lifetime Non-Merge Threshold</label>
            <input
              data-testid="direction-lifetime-non-merge-threshold"
              type="number"
              min={1}
              max={100000}
              value={lifetimeNonMergeThresholdInputVal}
              onChange={e => setLifetimeNonMergeThresholdInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(lifetimeNonMergeThresholdInputVal); if (!isNaN(n) && n >= 1 && n <= 100000) onUpdateDirectionLifetimeNonMergeThreshold(n); else setLifetimeNonMergeThresholdInputVal(String(directionLifetimeNonMergeThreshold)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(lifetimeNonMergeThresholdInputVal); if (!isNaN(n) && n >= 1 && n <= 100000) onUpdateDirectionLifetimeNonMergeThreshold(n); else setLifetimeNonMergeThresholdInputVal(String(directionLifetimeNonMergeThreshold)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Lifetime Min Merge Rate (0–1)</label>
            <input
              data-testid="direction-lifetime-min-merge-rate"
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={lifetimeMinMergeRateInputVal}
              onChange={e => setLifetimeMinMergeRateInputVal(e.target.value)}
              onBlur={() => { const n = parseFloat(lifetimeMinMergeRateInputVal); if (!isNaN(n) && n >= 0 && n <= 1) onUpdateDirectionLifetimeMinMergeRate(n); else setLifetimeMinMergeRateInputVal(String(directionLifetimeMinMergeRate)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseFloat(lifetimeMinMergeRateInputVal); if (!isNaN(n) && n >= 0 && n <= 1) onUpdateDirectionLifetimeMinMergeRate(n); else setLifetimeMinMergeRateInputVal(String(directionLifetimeMinMergeRate)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Review Recommend Threshold</label>
            <input
              data-testid="direction-review-recommendation-threshold"
              type="number"
              min={1}
              max={100}
              value={reviewRecommendThresholdInputVal}
              onChange={e => setReviewRecommendThresholdInputVal(e.target.value)}
              onBlur={() => { const n = parseInt(reviewRecommendThresholdInputVal); if (!isNaN(n) && n >= 1 && n <= 100) onUpdateDirectionReviewRecommendationThreshold(n); else setReviewRecommendThresholdInputVal(String(directionReviewRecommendationThreshold)); }}
              onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(reviewRecommendThresholdInputVal); if (!isNaN(n) && n >= 1 && n <= 100) onUpdateDirectionReviewRecommendationThreshold(n); else setReviewRecommendThresholdInputVal(String(directionReviewRecommendationThreshold)); } }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
        </div>
      </div>

      {/* Evergreen Fill */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Evergreen Fill</h3>
        <p className="text-xs text-neutral-500 mb-3">
          Automatically spawn backlog tasks when utilization is low. Use <em>observe</em> to see what would be created before enabling.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <div className="md:col-span-2 lg:col-span-3">
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Mode</label>
            <div className="flex gap-1 bg-neutral-950 border border-neutral-700 rounded-lg p-1 w-fit">
              {(['off', 'observe', 'enforce'] as const).map(mode => (
                <button
                  key={mode}
                  onClick={() => void patchFill({ evergreen_fill_mode: mode })}
                  className={`px-3 py-1.5 text-xs rounded-md transition-colors capitalize ${
                    fillSettings.evergreen_fill_mode === mode
                      ? 'bg-neutral-700 text-neutral-100'
                      : 'text-neutral-500 hover:text-neutral-300'
                  }`}
                >
                  {mode}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label htmlFor="fill-interval" className="block text-xs font-medium text-neutral-400 mb-1.5">Interval (1-1440 min)</label>
            <input
              id="fill-interval"
              type="number"
              min={1}
              max={1440}
              value={fillIntervalInput}
              onChange={e => setFillIntervalInput(e.target.value)}
              onBlur={() => commitFillNumber('evergreen_fill_interval_minutes', fillIntervalInput, 1, 1440, fillSettings.evergreen_fill_interval_minutes, setFillIntervalInput)}
              onKeyDown={e => { if (e.key === 'Enter') commitFillNumber('evergreen_fill_interval_minutes', fillIntervalInput, 1, 1440, fillSettings.evergreen_fill_interval_minutes, setFillIntervalInput); }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="fill-util-threshold" className="block text-xs font-medium text-neutral-400 mb-1.5">Util Threshold (0–1)</label>
            <input
              id="fill-util-threshold"
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={fillUtilInput}
              onChange={e => setFillUtilInput(e.target.value)}
              onBlur={() => commitFillNumber('evergreen_fill_util_threshold', fillUtilInput, 0, 1, fillSettings.evergreen_fill_util_threshold, setFillUtilInput)}
              onKeyDown={e => { if (e.key === 'Enter') commitFillNumber('evergreen_fill_util_threshold', fillUtilInput, 0, 1, fillSettings.evergreen_fill_util_threshold, setFillUtilInput); }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="fill-min-slots" className="block text-xs font-medium text-neutral-400 mb-1.5">Min Free Slots (0–100)</label>
            <input
              id="fill-min-slots"
              type="number"
              min={0}
              max={100}
              value={fillMinSlotsInput}
              onChange={e => setFillMinSlotsInput(e.target.value)}
              onBlur={() => commitFillNumber('evergreen_fill_min_free_slots', fillMinSlotsInput, 0, 100, fillSettings.evergreen_fill_min_free_slots, setFillMinSlotsInput)}
              onKeyDown={e => { if (e.key === 'Enter') commitFillNumber('evergreen_fill_min_free_slots', fillMinSlotsInput, 0, 100, fillSettings.evergreen_fill_min_free_slots, setFillMinSlotsInput); }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="fill-max-active" className="block text-xs font-medium text-neutral-400 mb-1.5">Max Active (0–50)</label>
            <input
              id="fill-max-active"
              type="number"
              min={0}
              max={50}
              value={fillMaxActiveInput}
              onChange={e => setFillMaxActiveInput(e.target.value)}
              onBlur={() => commitFillNumber('evergreen_fill_max_active', fillMaxActiveInput, 0, 50, fillSettings.evergreen_fill_max_active, setFillMaxActiveInput)}
              onKeyDown={e => { if (e.key === 'Enter') commitFillNumber('evergreen_fill_max_active', fillMaxActiveInput, 0, 50, fillSettings.evergreen_fill_max_active, setFillMaxActiveInput); }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="fill-daily-cap" className="block text-xs font-medium text-neutral-400 mb-1.5">Daily Cap (0–200)</label>
            <input
              id="fill-daily-cap"
              type="number"
              min={0}
              max={200}
              value={fillDailyCapInput}
              onChange={e => setFillDailyCapInput(e.target.value)}
              onBlur={() => commitFillNumber('evergreen_fill_daily_cap', fillDailyCapInput, 0, 200, fillSettings.evergreen_fill_daily_cap, setFillDailyCapInput)}
              onKeyDown={e => { if (e.key === 'Enter') commitFillNumber('evergreen_fill_daily_cap', fillDailyCapInput, 0, 200, fillSettings.evergreen_fill_daily_cap, setFillDailyCapInput); }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div className="flex items-end">
            <div className="w-full">
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Deferred-Memo Enrollment</label>
              <div className="flex items-center gap-3 bg-neutral-950 border border-neutral-700 rounded-lg px-3 py-2">
                <Toggle
                  enabled={fillSettings.evergreen_fill_deferred_memo_enrollment}
                  onChange={v => void patchFill({ evergreen_fill_deferred_memo_enrollment: v })}
                />
                <span className="text-xs text-neutral-400">
                  {fillSettings.evergreen_fill_deferred_memo_enrollment ? 'Enabled' : 'Disabled'}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Capability Proof Gate */}
      <div data-testid="capability-proof-gate-section">
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Capability Proof Gate</h3>
        <p className="text-xs text-neutral-500 mb-3">
          In <em>enforce</em> mode, capabilities without a <code>proven</code> proof no longer satisfy prerequisite
          resolution. Auto-graduation advances the mode (off&nbsp;→&nbsp;observe&nbsp;→&nbsp;enforce, forward-only)
          automatically once fleet proof coverage crosses the thresholds below.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <div className="md:col-span-2 lg:col-span-3">
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Mode</label>
            <div className="flex gap-1 bg-neutral-950 border border-neutral-700 rounded-lg p-1 w-fit" data-testid="proof-gate-mode">
              {(['off', 'observe', 'enforce'] as const).map(mode => (
                <button
                  key={mode}
                  onClick={() => void patchProofGateMode(mode)}
                  className={`px-3 py-1.5 text-xs rounded-md transition-colors capitalize ${
                    proofGateMode === mode
                      ? 'bg-neutral-700 text-neutral-100'
                      : 'text-neutral-500 hover:text-neutral-300'
                  }`}
                >
                  {mode}
                </button>
              ))}
            </div>
          </div>
          <div className="flex items-end">
            <div className="w-full">
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Auto-Graduation</label>
              <div className="flex items-center gap-3 bg-neutral-950 border border-neutral-700 rounded-lg px-3 py-2">
                <Toggle enabled={proofAutoGradEnabled} onChange={v => void patchProofAutoGrad(v)} />
                <span className="text-xs text-neutral-400">{proofAutoGradEnabled ? 'Enabled' : 'Disabled'}</span>
              </div>
            </div>
          </div>
          <div>
            <label htmlFor="proof-gate-observe-threshold" className="block text-xs font-medium text-neutral-400 mb-1.5">Observe Threshold (0–1)</label>
            <input
              id="proof-gate-observe-threshold"
              data-testid="proof-gate-observe-threshold"
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={proofObserveThresholdInput}
              onChange={e => setProofObserveThresholdInput(e.target.value)}
              onBlur={() => commitProofThreshold('capability_proof_gate_auto_graduation_observe_threshold', proofObserveThresholdInput, proofObserveThreshold, setProofObserveThreshold, setProofObserveThresholdInput)}
              onKeyDown={e => { if (e.key === 'Enter') commitProofThreshold('capability_proof_gate_auto_graduation_observe_threshold', proofObserveThresholdInput, proofObserveThreshold, setProofObserveThreshold, setProofObserveThresholdInput); }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label htmlFor="proof-gate-enforce-threshold" className="block text-xs font-medium text-neutral-400 mb-1.5">Enforce Threshold (0–1)</label>
            <input
              id="proof-gate-enforce-threshold"
              data-testid="proof-gate-enforce-threshold"
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={proofEnforceThresholdInput}
              onChange={e => setProofEnforceThresholdInput(e.target.value)}
              onBlur={() => commitProofThreshold('capability_proof_gate_auto_graduation_enforce_threshold', proofEnforceThresholdInput, proofEnforceThreshold, setProofEnforceThreshold, setProofEnforceThresholdInput)}
              onKeyDown={e => { if (e.key === 'Enter') commitProofThreshold('capability_proof_gate_auto_graduation_enforce_threshold', proofEnforceThresholdInput, proofEnforceThreshold, setProofEnforceThreshold, setProofEnforceThresholdInput); }}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
        </div>
        {proofObserveThreshold > proofEnforceThreshold && (
          <p className="text-ui-sm text-amber-400 mt-2" data-testid="proof-gate-threshold-warning">
            Observe threshold should be ≤ enforce threshold — auto-graduation advances off → observe → enforce.
          </p>
        )}
      </div>

      {/* Evergreen Fill Observability */}
      <EvergreenFillObservabilityPanel />

      {/* Structural Health Observability */}
      <StructuralHealthObservabilityPanel auditMode={auditMode} onAuditModeChange={patchAuditMode} />

      {/* Metrics Integrity Observability */}
      <MetricsIntegrityObservabilityPanel sweepMode={sweepMode} onSweepModeChange={patchSweepMode} />

      {/* Evergreen Backlog (project-scoped) */}
      <EvergreenBacklogPanel projectId={selectedProjectId} />

      {/* Restart */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Danger Zone</h3>
        <button
          disabled={restarting}
          onClick={async () => {
            if (!window.confirm('Restart the orchestrator? In-flight task state will be preserved.')) return;
            setRestarting(true);
            setRestartStatus('idle');
            try {
              await onRestartOrchestrator();
              setRestartStatus('success');
            } catch {
              setRestartStatus('error');
            } finally {
              setRestarting(false);
            }
          }}
          className="bg-neutral-800 hover:bg-amber-900/30 disabled:opacity-50 border border-neutral-700 text-amber-400 text-xs py-2 px-4 rounded-lg transition-colors flex items-center gap-2"
        >
          {restarting
            ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Restarting&hellip;</>
            : <><RotateCcw className="w-3.5 h-3.5" /> Restart Orchestrator</>}
        </button>
        {restartStatus === 'success' && <p className="text-xs text-emerald-400 mt-2">Orchestrator restarted successfully.</p>}
        {restartStatus === 'error' && <p className="text-xs text-red-400 mt-2">Restart failed. Check console for details.</p>}
      </div>
    </div>
  );
}
