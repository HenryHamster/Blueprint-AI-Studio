import type React from 'react';
import { Check, ChevronDown, ChevronRight, Settings } from 'lucide-react';

export interface AutonomousConfigSectionProps {
  configCooldownHours: number;
  setConfigCooldownHours: (v: number) => void;
  configProductiveCooldownMinutes: number;
  setConfigProductiveCooldownMinutes: (v: number) => void;
  configIntervalMinutes: number;
  setConfigIntervalMinutes: (v: number) => void;
  configMaxParallel: number;
  setConfigMaxParallel: (v: number) => void;
  configMaxTotalResearch: number;
  setConfigMaxTotalResearch: (v: number) => void;
  configMaxTasksPerHour: number;
  setConfigMaxTasksPerHour: (v: number) => void;
  configMaxConcurrentAutoRemediations: number;
  setConfigMaxConcurrentAutoRemediations: (v: number) => void;
  configResidualRemediationCooldownHours: number;
  setConfigResidualRemediationCooldownHours: (v: number) => void;
  configResidualAuditDeferEscalationThreshold: number;
  setConfigResidualAuditDeferEscalationThreshold: (v: number) => void;
  configMaxActiveDirections: number;
  setConfigMaxActiveDirections: (v: number) => void;
  configArchitectureReviewFrequency: number;
  setConfigArchitectureReviewFrequency: (v: number) => void;
  configStrategicMaxIntervalMinutes: number;
  setConfigStrategicMaxIntervalMinutes: (v: number) => void;
  configMaxProvisionRetries: number;
  setConfigMaxProvisionRetries: (v: number) => void;
  configMaxRollupRetries: number;
  setConfigMaxRollupRetries: (v: number) => void;
  configDriftOverClaimMinClaims: number;
  setConfigDriftOverClaimMinClaims: (v: number) => void;
  configDriftConsistencyThreshold: number;
  setConfigDriftConsistencyThreshold: (v: number) => void;
  configDriftStallMinClaims: number;
  setConfigDriftStallMinClaims: (v: number) => void;
  autonomousConfigOpen: boolean;
  setAutonomousConfigOpen: React.Dispatch<React.SetStateAction<boolean>>;
  savingConfigField: string | null;
  saveConfigField: (field: string, value: number) => Promise<void>;
  clearResidualFingerprints: () => Promise<number | null>;
}

export function AutonomousConfigSection({
  configCooldownHours, setConfigCooldownHours,
  configProductiveCooldownMinutes, setConfigProductiveCooldownMinutes,
  configIntervalMinutes, setConfigIntervalMinutes,
  configMaxParallel, setConfigMaxParallel,
  configMaxTotalResearch, setConfigMaxTotalResearch,
  configMaxTasksPerHour, setConfigMaxTasksPerHour,
  configMaxConcurrentAutoRemediations, setConfigMaxConcurrentAutoRemediations,
  configResidualRemediationCooldownHours, setConfigResidualRemediationCooldownHours,
  configResidualAuditDeferEscalationThreshold, setConfigResidualAuditDeferEscalationThreshold,
  configMaxActiveDirections, setConfigMaxActiveDirections,
  configArchitectureReviewFrequency, setConfigArchitectureReviewFrequency,
  configStrategicMaxIntervalMinutes, setConfigStrategicMaxIntervalMinutes,
  configMaxProvisionRetries, setConfigMaxProvisionRetries,
  configMaxRollupRetries, setConfigMaxRollupRetries,
  configDriftOverClaimMinClaims, setConfigDriftOverClaimMinClaims,
  configDriftConsistencyThreshold, setConfigDriftConsistencyThreshold,
  configDriftStallMinClaims, setConfigDriftStallMinClaims,
  autonomousConfigOpen, setAutonomousConfigOpen,
  savingConfigField, saveConfigField,
  clearResidualFingerprints,
}: AutonomousConfigSectionProps) {
  return (
    <div>
      {/* reason: collapsible autonomous config section toggle; text-ui-md single-row height matches panel density */}
      <button
        data-viewport-allow-small-target
        onClick={() => setAutonomousConfigOpen(v => !v)}
        className="flex items-center gap-1.5 w-full text-left group"
      >
        <Settings className="w-3 h-3 text-neutral-500 group-hover:text-neutral-400 transition-colors" />
        <span className="text-ui-md font-medium text-neutral-500 uppercase tracking-wider group-hover:text-neutral-400 transition-colors flex-1">
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
            <label className="text-ui-md text-neutral-400 w-32 shrink-0">Cooldown (hours)</label>
            <input
              type="number"
              min={1}
              max={720}
              value={configCooldownHours}
              onChange={e => setConfigCooldownHours(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
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

          {/* Productive direction cooldown (min) */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Shorter cooldown applied to directions whose previous cycle was productive (spawned merged work), controlling how quickly successful directions get re-fed. Stored in minutes (default 45).">Productive cooldown (min)</label>
            <input
              type="number"
              min={1}
              max={525600}
              value={configProductiveCooldownMinutes}
              onChange={e => setConfigProductiveCooldownMinutes(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('productive_direction_cooldown_minutes', configProductiveCooldownMinutes)}
              disabled={savingConfigField === 'productive_direction_cooldown_minutes'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save (default: 45)"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Poll interval (min) */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0">Poll interval (min)</label>
            <input
              type="number"
              min={5}
              max={1440}
              value={configIntervalMinutes}
              onChange={e => setConfigIntervalMinutes(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
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
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Max concurrent research/plan tasks spawned per project per autonomy cycle (counts QUEUED/PROVISIONING/IN_PROGRESS only — controls active dispatch concurrency)">Max parallel</label>
            <input
              type="number"
              min={1}
              max={20}
              value={configMaxParallel}
              onChange={e => setConfigMaxParallel(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
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

          {/* Max total research per project (includes EVALUATING) */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Max total research tasks per project across ALL non-terminal states (QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING). Throttles pending-rollup pile-up — each EVALUATING parent has children waiting to merge, and stacking too many makes downstream merges progressively more conflict-prone.">Max total research</label>
            <input
              type="number"
              min={1}
              max={50}
              value={configMaxTotalResearch}
              onChange={e => setConfigMaxTotalResearch(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('max_total_research_per_project', configMaxTotalResearch)}
              disabled={savingConfigField === 'max_total_research_per_project'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Max tasks/hour */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0">Max tasks/hour</label>
            <input
              type="number"
              min={1}
              max={50}
              value={configMaxTasksPerHour}
              onChange={e => setConfigMaxTasksPerHour(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
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
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Max concurrent auto-remediation tasks (spawned from residual CODE_ERROR fingerprints). Prevents queue ballooning when many distinct fingerprints accumulate.">Max auto-remed</label>
            <input
              type="number"
              min={0}
              max={20}
              value={configMaxConcurrentAutoRemediations}
              onChange={e => setConfigMaxConcurrentAutoRemediations(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
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
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Per-fingerprint cooldown between auto-remediation spawns. Prevents re-spawning while a prior remediation is still running or recently merged.">Remed cooldown (h)</label>
            <input
              type="number"
              min={1}
              max={720}
              value={configResidualRemediationCooldownHours}
              onChange={e => setConfigResidualRemediationCooldownHours(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
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

          {/* Residual audit-defer escalation threshold */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Number of times a residual auto-remediation case may be deferred by an in-progress audit before the system escalates to operators via the 'Audit Deferred' notification (default 5).">Audit defer limit</label>
            <input
              type="number"
              min={1}
              max={50}
              value={configResidualAuditDeferEscalationThreshold}
              onChange={e => setConfigResidualAuditDeferEscalationThreshold(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('residual_audit_defer_escalation_threshold', configResidualAuditDeferEscalationThreshold)}
              disabled={savingConfigField === 'residual_audit_defer_escalation_threshold'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save (default: 5)"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Max active directions */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Maximum number of active (non-paused, non-retired) directions at once.">Max directions</label>
            <input
              type="number"
              min={1}
              max={50}
              value={configMaxActiveDirections}
              onChange={e => setConfigMaxActiveDirections(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('max_active_directions', configMaxActiveDirections)}
              disabled={savingConfigField === 'max_active_directions'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Architecture review frequency */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Run an architecture review every N strategic cycles. 0 = disabled.">Arch review (cycles)</label>
            <input
              type="number"
              min={0}
              max={100}
              value={configArchitectureReviewFrequency}
              onChange={e => setConfigArchitectureReviewFrequency(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('architecture_review_frequency', configArchitectureReviewFrequency)}
              disabled={savingConfigField === 'architecture_review_frequency'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Strategic wall-clock backstop */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Force a strategic cycle if this many minutes have elapsed since the last one, regardless of research-spawn count. Prevents the strategic agent's steering frequency from collapsing when it narrows direction count. 0 = disabled.">Strategic max gap (min)</label>
            <input
              type="number"
              min={0}
              max={10080}
              value={configStrategicMaxIntervalMinutes}
              onChange={e => setConfigStrategicMaxIntervalMinutes(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('strategic_max_interval_minutes', configStrategicMaxIntervalMinutes)}
              disabled={savingConfigField === 'strategic_max_interval_minutes'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Max provision retries */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Max VM provisioning retries before marking a task as failed (1–10).">Provision retries</label>
            <input
              type="number"
              min={1}
              max={10}
              value={configMaxProvisionRetries}
              onChange={e => setConfigMaxProvisionRetries(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('max_provision_retries', configMaxProvisionRetries)}
              disabled={savingConfigField === 'max_provision_retries'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Max rollup retries */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Max research rollup retries before abandoning (1–20).">Rollup retries</label>
            <input
              type="number"
              min={1}
              max={20}
              value={configMaxRollupRetries}
              onChange={e => setConfigMaxRollupRetries(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('max_rollup_retries', configMaxRollupRetries)}
              disabled={savingConfigField === 'max_rollup_retries'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Drift detection thresholds */}
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Minimum task claims in window before over-claim flag fires (default 3).">Drift claim min</label>
            <input
              type="number"
              min={1}
              max={50}
              value={configDriftOverClaimMinClaims}
              onChange={e => setConfigDriftOverClaimMinClaims(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('drift_over_claim_min_claims', configDriftOverClaimMinClaims)}
              disabled={savingConfigField === 'drift_over_claim_min_claims'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save (default: 3)"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Consistency ratio below which over-claim flag fires (default 0.4 = 40% of claims merged).">Drift consistency</label>
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={configDriftConsistencyThreshold}
              onChange={e => setConfigDriftConsistencyThreshold(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('drift_over_claim_consistency_threshold', configDriftConsistencyThreshold)}
              disabled={savingConfigField === 'drift_over_claim_consistency_threshold'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save (default: 0.4)"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Minimum claims with zero merges before stalled flag fires (default 5).">Drift stall min</label>
            <input
              type="number"
              min={1}
              max={50}
              value={configDriftStallMinClaims}
              onChange={e => setConfigDriftStallMinClaims(Number(e.target.value))}
              className="flex-1 bg-workbench-base border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm workbench-focus"
            />
            <button
              onClick={() => saveConfigField('drift_stall_min_claims', configDriftStallMinClaims)}
              disabled={savingConfigField === 'drift_stall_min_claims'}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 transition-colors shrink-0"
              title="Save (default: 5)"
            >
              <Check className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* Clear residual fingerprints — useful after misclassification regime change */}
          <div className="flex items-center gap-2 pt-1">
            <label className="text-ui-md text-neutral-400 w-32 shrink-0" title="Wipes the in-memory residual CODE_ERROR fingerprint store. Use when the store is polluted by bad classifications; new failures will repopulate it.">Fingerprints</label>
            <button
              onClick={async () => {
                if (!window.confirm('Wipe all residual CODE_ERROR fingerprints from memory? This only affects the in-memory store used for auto-remediation spawn decisions.')) return;
                await clearResidualFingerprints();
              }}
              className="flex-1 text-left bg-workbench-base border border-neutral-700 hover:border-red-600/50 text-neutral-300 hover:text-red-400 rounded px-3 py-1.5 text-xs transition-colors"
            >
              Clear all residual fingerprints
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
