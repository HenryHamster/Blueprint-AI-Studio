/**
 * Narrow dependency facade for the discovery-review handler.
 *
 * Keeping the handler's infrastructure imports behind one explicit surface
 * makes its contract tests complete without replacing the enormous index and
 * prompt module APIs with partial mocks.
 */
export {
  WORKSPACE,
  git,
  persistAndPostStatus,
  resolveBaseBranch,
  runClaudeCode,
  sanitize,
  sendHeartbeatSync,
  sendLog,
  setCurrentPromptFingerprint,
  strVal,
  validateBranchName,
} from '../index.js';
export { prepareWorkspaceForRole } from '../workspacePrep.js';
export { preparePacketOnlyWorkspace } from './discovery-review-workspace.js';
export {
  DEFAULT_TOOL_GUIDANCE,
  buildContextPrefix,
  computePromptFingerprint,
  configureAgentPermissions,
  readClaudeMd,
} from '../prompt.js';
export { providerAutoInjectsClaudeMd } from '../providers/runners.js';
export {
  buildPromptSizeTelemetry,
  setPromptSizeTelemetry,
} from '../telemetry.js';
