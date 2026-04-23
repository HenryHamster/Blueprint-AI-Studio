import type React from 'react';
import { Check, ChevronDown, ChevronRight, Settings } from 'lucide-react';

export interface AutonomousConfigSectionProps {
  configCooldownHours: number;
  setConfigCooldownHours: (v: number) => void;
  configIntervalMinutes: number;
  setConfigIntervalMinutes: (v: number) => void;
  configMaxParallel: number;
  setConfigMaxParallel: (v: number) => void;
  configMaxTasksPerHour: number;
  setConfigMaxTasksPerHour: (v: number) => void;
  configMaxConcurrentAutoRemediations: number;
  setConfigMaxConcurrentAutoRemediations: (v: number) => void;
  configResidualRemediationCooldownHours: number;
  setConfigResidualRemediationCooldownHours: (v: number) => void;
  autonomousConfigOpen: boolean;
  setAutonomousConfigOpen: React.Dispatch<React.SetStateAction<boolean>>;
  savingConfigField: string | null;
  saveConfigField: (field: string, value: number) => Promise<void>;
  clearResidualFingerprints: () => Promise<number | null>;
}

export function AutonomousConfigSection({
  configCooldownHours, setConfigCooldownHours,
  configIntervalMinutes, setConfigIntervalMinutes,
  configMaxParallel, setConfigMaxParallel,
  configMaxTasksPerHour, setConfigMaxTasksPerHour,
  configMaxConcurrentAutoRemediations, setConfigMaxConcurrentAutoRemediations,
  configResidualRemediationCooldownHours, setConfigResidualRemediationCooldownHours,
  autonomousConfigOpen, setAutonomousConfigOpen,
  savingConfigField, saveConfigField,
  clearResidualFingerprints,
}: AutonomousConfigSectionProps) {
  return (
    <div>
      <button
        onClick={() => setAutonomousConfigOpen(v => !v)}
        className="flex items-center gap-1.5 w-full text-left group"
      >
        <Settings className="w-3 h-3 text-neutral-500 group-hover:text-neutral-400 transition-colors" />
        <span className="text-[11px] font-medium text-neutral-500 uppercase tracking-wider group-hover:text-neutral-400 transition-colors flex-1">
          Autonomous Config
        </span>
        {autonomousConfigOpen
          ? <ChevronDown className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
          : <ChevronRight className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
        }
      </button>

      {autonomousConfigOpen && (
        <div className="mt-2 space-y-2">
          {/* Cooldown (hours) */}
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-neutral-400 w-32 shrink-0">Cooldown (hours)</label>
            <input
              type="number"
              min={1}
              max={720}
              value={configCooldownHours}
              onChange={e => setConfigCooldownHours(Number(e.target.value))}
              className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <button
              onClick={() => saveConfigField('autonomous_direction_cooldown_hours', configCooldownHours)}
              disabled={savingConfigField === 'autonomous_direction_cooldown_hours'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Poll interval (min) */}
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-neutral-400 w-32 shrink-0">Poll interval (min)</label>
            <input
              type="number"
              min={5}
              max={1440}
              value={configIntervalMinutes}
              onChange={e => setConfigIntervalMinutes(Number(e.target.value))}
              className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <button
              onClick={() => saveConfigField('autonomous_research_interval_minutes', configIntervalMinutes)}
              disabled={savingConfigField === 'autonomous_research_interval_minutes'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Max parallel research tasks per project per cycle */}
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-neutral-400 w-32 shrink-0" title="Max concurrent research/plan tasks spawned per project per autonomy cycle">Max parallel</label>
            <input
              type="number"
              min={1}
              max={20}
              value={configMaxParallel}
              onChange={e => setConfigMaxParallel(Number(e.target.value))}
              className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <button
              onClick={() => saveConfigField('max_parallel_research_per_project', configMaxParallel)}
              disabled={savingConfigField === 'max_parallel_research_per_project'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Max tasks/hour */}
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-neutral-400 w-32 shrink-0">Max tasks/hour</label>
            <input
              type="number"
              min={1}
              max={50}
              value={configMaxTasksPerHour}
              onChange={e => setConfigMaxTasksPerHour(Number(e.target.value))}
              className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <button
              onClick={() => saveConfigField('max_autonomous_tasks_per_hour', configMaxTasksPerHour)}
              disabled={savingConfigField === 'max_autonomous_tasks_per_hour'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Auto-remediation concurrency cap */}
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-neutral-400 w-32 shrink-0" title="Max concurrent auto-remediation tasks (spawned from residual CODE_ERROR fingerprints). Prevents queue ballooning when many distinct fingerprints accumulate.">Max auto-remed</label>
            <input
              type="number"
              min={0}
              max={20}
              value={configMaxConcurrentAutoRemediations}
              onChange={e => setConfigMaxConcurrentAutoRemediations(Number(e.target.value))}
              className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <button
              onClick={() => saveConfigField('max_concurrent_auto_remediations', configMaxConcurrentAutoRemediations)}
              disabled={savingConfigField === 'max_concurrent_auto_remediations'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Residual fingerprint cooldown (hours) */}
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-neutral-400 w-32 shrink-0" title="Per-fingerprint cooldown between auto-remediation spawns. Prevents re-spawning while a prior remediation is still running or recently merged.">Remed cooldown (h)</label>
            <input
              type="number"
              min={1}
              max={720}
              value={configResidualRemediationCooldownHours}
              onChange={e => setConfigResidualRemediationCooldownHours(Number(e.target.value))}
              className="flex-1 bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
            <button
              onClick={() => saveConfigField('residual_remediation_cooldown_hours', configResidualRemediationCooldownHours)}
              disabled={savingConfigField === 'residual_remediation_cooldown_hours'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Clear residual fingerprints — useful after misclassification regime change */}
          <div className="flex items-center gap-2 pt-1">
            <label className="text-[11px] text-neutral-400 w-32 shrink-0" title="Wipes the in-memory residual CODE_ERROR fingerprint store. Use when the store is polluted by bad classifications; new failures will repopulate it.">Fingerprints</label>
            <button
              onClick={async () => {
                if (!window.confirm('Wipe all residual CODE_ERROR fingerprints from memory? This only affects the in-memory store used for auto-remediation spawn decisions.')) return;
                await clearResidualFingerprints();
              }}
              className="flex-1 text-left bg-neutral-900 border border-neutral-700 hover:border-red-600/50 text-neutral-300 hover:text-red-400 rounded px-3 py-1.5 text-xs transition-colors"
            >
              Clear all residual fingerprints
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
