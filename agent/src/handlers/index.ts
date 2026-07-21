/**
 * Task handler registry and dispatcher.
 *
 * Maps task types to their corresponding handler functions. Each handler processes
 * a specific task type (create, refactor, docs, review, fix, research, plan, etc.)
 * and manages the task lifecycle: cloning the repo, setting up the workspace,
 * running Claude Code, capturing output, and reporting status back to the orchestrator.
 *
 * The handlers module is the central dispatch point for all task execution logic.
 * New task types must be added to both the import statements and the `handlers` map
 * to be discoverable by the agent runtime.
 *
 * Research tasks are special: they dispatch to either the tactical research handler
 * or the strategic handler based on the task's `research_mode` field (set via the
 * dispatchResearch function).
 */

import type WebSocket from 'ws';
import type { AgentTaskType } from '../taskCapabilities.js';
import { handleCreate, handleRefactor, handleDocs, handleDeps, handleQA, handleSalvageBundle } from './standard.js';
import { handleReview, handleFix } from './review-fix.js';
import { handlePanelReview } from './panel-review.js';
import { handlePlan, handleResearch } from './research-plan.js';
import { handleStrategic } from './strategic.js';
import { handleSecurityAudit, handleSelfAudit, handleReleaseAudit } from './audits.js';
import { handleReleaseAuditChallenge } from './audit-challenge.js';
import { handleReleaseMergeGrade } from './merge-grade.js';
import { handleMigrate, handleSmoketest, handleDebug, handlePerf, handleInvestigate, handleRepoBootstrap } from './specialized.js';
import { handleDesignDocUpdate } from './design-doc.js';
import { handleMemo } from './memo.js';
import { handleVerifyTests } from './verify-tests.js';
import { handleComputeExperiment } from './compute-experiment.js';
import { handleWebResearch } from './web-research.js';
import { handleDesign } from './design.js';
import { handleCapabilityProofTrial } from './capability-proof-trial.js';
import {
  resolveTaskCapabilityContract,
  resolveGenericRunner,
  type TaskCapabilityContract,
} from './contract-dispatch.js';

export {
  resolveTaskCapabilityContract,
  resolveGenericRunner,
  type TaskCapabilityContract,
  type GenericRunner,
} from './contract-dispatch.js';

export type Handler = (
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) => Promise<void>;

// strategic_review (and research_mode === 'strategic') dispatch: routes to the
// one-shot handleStrategic behavior.
const dispatchStrategic: Handler = (taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort) => {
  return handleStrategic(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
};

// research task type forwards to the strategic handler when research_mode is
// 'strategic'; otherwise it runs the tactical research path. strategic_review
// always goes through dispatchStrategic.
const dispatchResearch: Handler = (taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort) => {
  if (payload.research_mode === 'strategic') {
    return dispatchStrategic(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
  }
  return handleResearch(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
};

export const handlers: Record<AgentTaskType, Handler> = {
  create: handleCreate,
  review: handleReview,
  panel_review: handlePanelReview,
  fix: handleFix,
  plan: handlePlan,
  research: dispatchResearch,
  docs: handleDocs,
  security_audit: handleSecurityAudit,
  self_audit: handleSelfAudit,
  qa: handleQA,
  deps: handleDeps,
  migrate: handleMigrate,
  smoketest: handleSmoketest,
  refactor: handleRefactor,
  debug: handleDebug,
  perf: handlePerf,
  investigate: handleInvestigate,
  design_doc_update: handleDesignDocUpdate,
  strategic_review: dispatchStrategic,
  release_audit: handleReleaseAudit,
  release_audit_challenge: handleReleaseAuditChallenge,
  release_merge_grade: handleReleaseMergeGrade,
  memo: handleMemo,
  verify_tests: handleVerifyTests,
  compute_experiment: handleComputeExperiment,
  web_research: handleWebResearch,
  design: handleDesign,
  repo_bootstrap: handleRepoBootstrap,
  salvage_bundle: handleSalvageBundle,
  capability_proof_trial: handleCapabilityProofTrial,
};

// ── Contract-driven handler resolution ───────────────────────────────────────
// The result of resolving a claimed task type to a runner. A type with a BESPOKE
// handler keeps all its current behavior; otherwise a GENERIC runner is resolved
// from the published task_capability contract. `handler: null` means no runner
// exists for the contract yet (e.g. the reserved experiment_loop, or any
// unrecognized run-shape) — the caller must fail loudly with `reason`.
export type HandlerResolution =
  | { handler: Handler; dispatch: 'bespoke' }
  | { handler: Handler; dispatch: 'generic'; runnerLabel: string }
  | { handler: null; dispatch: 'none'; reason: string };

/**
 * Resolve the runner for a claimed task. Bespoke handlers win (exact-type match);
 * otherwise a generic runner is selected from the capability contract so a newly
 * registered task type of a known run-shape is runnable without an agent edit.
 */
export function resolveHandler(
  taskType: string,
  task: Record<string, unknown>,
  payload: Record<string, unknown>,
): HandlerResolution {
  const bespoke = (handlers as Partial<Record<string, Handler>>)[taskType];
  if (bespoke) return { handler: bespoke, dispatch: 'bespoke' };

  const contract: TaskCapabilityContract = resolveTaskCapabilityContract(task, payload);
  const generic = resolveGenericRunner(contract);
  if (generic) {
    return { handler: generic.handler, dispatch: 'generic', runnerLabel: generic.label };
  }

  const supported = Object.keys(handlers).sort().join(', ');
  return {
    handler: null,
    dispatch: 'none',
    reason:
      `Unknown task type "${taskType}" with no generic runner for run_shape=${contract.run_shape}, ` +
      `agent_mode=${contract.agent_mode}, output_kind=${contract.output_kind}. ` +
      `Generic dispatch supports single_agent read_only→knowledge/none ` +
      `and workflow write→pr. Bespoke handlers: ${supported}`,
  };
}
