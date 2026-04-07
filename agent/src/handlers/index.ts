import type WebSocket from 'ws';
import { handleCreate, handleRefactor, handleDocs, handleDeps, handleQA } from './standard.js';
import { handleReview, handleFix } from './review-fix.js';
import { handlePlan, handleResearch } from './research-plan.js';
import { handleSecurityAudit, handleMigrate, handleSmoketest, handleDebug, handlePerf, handleInvestigate } from './specialized.js';

export type Handler = (
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) => Promise<void>;

export const handlers: Record<string, Handler> = {
  create: handleCreate,
  review: handleReview,
  fix: handleFix,
  plan: handlePlan,
  research: handleResearch,
  docs: handleDocs,
  security_audit: handleSecurityAudit,
  qa: handleQA,
  deps: handleDeps,
  migrate: handleMigrate,
  smoketest: handleSmoketest,
  refactor: handleRefactor,
  debug: handleDebug,
  perf: handlePerf,
  investigate: handleInvestigate,
};
