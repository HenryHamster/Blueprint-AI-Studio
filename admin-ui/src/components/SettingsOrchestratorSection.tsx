import { useEffect, useState } from 'react';
import { Loader2, RotateCcw } from 'lucide-react';

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
}

const RESERVED_SLOT_TYPES = ['create', 'research', 'plan'] as const;

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
}: SettingsOrchestratorSectionProps) {
  const [vmInputVal, setVmInputVal] = useState(String(maxConcurrentVMs));
  const [retriesInputVal, setRetriesInputVal] = useState(String(maxRetries));
  const [autonomousRateInputVal, setAutonomousRateInputVal] = useState(String(maxAutonomousTasksPerHour));
  const [globalRateInputVal, setGlobalRateInputVal] = useState(String(maxGlobalAutonomousTasksPerHour));
  const [pipelineSlotsInputVal, setPipelineSlotsInputVal] = useState(String(maxPipelineSlots));
  const [reservedInputVals, setReservedInputVals] = useState<Record<string, string>>(() =>
    Object.fromEntries(RESERVED_SLOT_TYPES.map(t => [t, String(reservedSlotsForTypes[t] ?? 0)])),
  );
  const [evalTimeoutInputVal, setEvalTimeoutInputVal] = useState(String(evaluatingPipelineTimeoutMinutes));
  const [evalFailHoursInputVal, setEvalFailHoursInputVal] = useState(String(evaluatingFailHours));
  const [ciBackstopInputVal, setCiBackstopInputVal] = useState(String(ciBackstopTimeoutMinutes));
  const [restarting, setRestarting] = useState(false);
  const [restartStatus, setRestartStatus] = useState<'idle' | 'success' | 'error'>('idle');

  useEffect(() => { setVmInputVal(String(maxConcurrentVMs)); }, [maxConcurrentVMs]);
  useEffect(() => { setRetriesInputVal(String(maxRetries)); }, [maxRetries]);
  useEffect(() => { setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); }, [maxAutonomousTasksPerHour]);
  useEffect(() => { setGlobalRateInputVal(String(maxGlobalAutonomousTasksPerHour)); }, [maxGlobalAutonomousTasksPerHour]);
  useEffect(() => { setPipelineSlotsInputVal(String(maxPipelineSlots)); }, [maxPipelineSlots]);
  useEffect(() => {
    setReservedInputVals(Object.fromEntries(RESERVED_SLOT_TYPES.map(t => [t, String(reservedSlotsForTypes[t] ?? 0)])));
  }, [reservedSlotsForTypes]);
  useEffect(() => { setEvalTimeoutInputVal(String(evaluatingPipelineTimeoutMinutes)); }, [evaluatingPipelineTimeoutMinutes]);
  useEffect(() => { setEvalFailHoursInputVal(String(evaluatingFailHours)); }, [evaluatingFailHours]);
  useEffect(() => { setCiBackstopInputVal(String(ciBackstopTimeoutMinutes)); }, [ciBackstopTimeoutMinutes]);

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
          {RESERVED_SLOT_TYPES.map(t => (
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
              value={autoArchiveTerminalAfterDays}
              onChange={e => onUpdateAutoArchiveTerminalAfterDays(parseInt(e.target.value) || 7)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Per-Project Cap (0=off)</label>
            <input
              type="number"
              min={0}
              max={10000}
              value={autoArchiveMaxTerminalTasksPerProject}
              onChange={e => onUpdateAutoArchiveMaxTerminalTasksPerProject(parseInt(e.target.value) || 0)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Interval (5-1440 min)</label>
            <input
              type="number"
              min={5}
              max={1440}
              value={autoArchiveIntervalMinutes}
              onChange={e => onUpdateAutoArchiveIntervalMinutes(parseInt(e.target.value) || 60)}
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
              value={autonomousResearchIntervalMinutes}
              onChange={e => onUpdateAutonomousResearchInterval(parseInt(e.target.value) || 30)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Direction Cooldown (min)</label>
            <input
              type="number"
              min={1}
              max={10080}
              value={directionCooldownMinutes}
              onChange={e => onUpdateDirectionCooldownMinutes(parseInt(e.target.value) || 240)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-neutral-400 mb-1.5">Strategic Freq (0=off)</label>
            <input
              type="number"
              min={0}
              max={100}
              value={strategicResearchFrequency}
              onChange={e => onUpdateStrategicResearchFrequency(parseInt(e.target.value) || 0)}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <p className="text-[10px] text-neutral-600 mt-1">Every Nth cycle reviews vision</p>
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
