import type WebSocket from 'ws';
import type { AgentTaskType } from '../taskCapabilities.js';
import { handleCreate, handleRefactor, handleDocs, handleDeps, handleQA } from './standard.js';
import { handleReview, handleFix } from './review-fix.js';
import { handlePlan, handleResearch } from './research-plan.js';
import { handleStrategic } from './strategic.js';
import { handleSecurityAudit, handleSelfAudit, handleReleaseAudit } from './audits.js';
import { handleMigrate, handleSmoketest, handleDebug, handlePerf, handleInvestigate } from './specialized.js';
import { handleDesignDocUpdate } from './design-doc.js';
import { handleMemo } from './memo.js';
import { handleVerifyTests } from './verify-tests.js';
import { handleComputeExperiment } from './compute-experiment.js';

export type Handler = (
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) => Promise<void>;

// research task type forwards to the strategic handler when research_mode is
// 'strategic'; otherwise it runs the tactical research path. strategic_review
// always goes directly to handleStrategic.
const dispatchResearch: Handler = (taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort) => {
  if (payload.research_mode === 'strategic') {
    return handleStrategic(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
  }
  return handleResearch(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
};

export const handlers: Record<AgentTaskType, Handler> = {
  create: handleCreate,
  review: handleReview,
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
  strategic_review: handleStrategic,
  release_audit: handleReleaseAudit,
  memo: handleMemo,
  verify_tests: handleVerifyTests,
  compute_experiment: handleComputeExperiment,
};
