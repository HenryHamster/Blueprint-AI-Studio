import type WebSocket from 'ws';
import type { AgentTaskType } from '../taskCapabilities.js';
import { handleCreate, handleRefactor, handleDocs, handleDeps, handleQA } from './standard.js';
import { handleReview, handleFix } from './review-fix.js';
import { handlePlan, handleResearch } from './research-plan.js';
import { handleSecurityAudit, handleMigrate, handleSmoketest, handleDebug, handlePerf, handleInvestigate, handleSelfAudit, handleReleaseAudit } from './specialized.js';
import { handleDesignDocUpdate } from './design-doc.js';

export type Handler = (
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) => Promise<void>;

export const handlers: Record<AgentTaskType, Handler> = {
  create: handleCreate,
  review: handleReview,
  fix: handleFix,
  plan: handlePlan,
  research: handleResearch,
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
  strategic_review: handleResearch,  // Reuses research handler; branches on research_mode
  release_audit: handleReleaseAudit,
};
