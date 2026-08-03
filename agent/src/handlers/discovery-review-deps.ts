/**
 * Narrow dependency facade for the discovery-review handler.
 *
 * Keeping the handler's infrastructure imports behind one explicit surface
 * makes its contract tests complete without replacing the enormous index and
 * prompt module APIs with partial mocks.
 */
export {
  persistAndPostStatus,
  sanitize,
  sendHeartbeatSync,
  sendLog,
  setCurrentPromptFingerprint,
  strVal,
} from '../index.js';
export { runIsolatedTextOnlyMemory } from '../providers/runners.js';
export {
  cleanupPacketOnlyWorkspace,
  preparePacketOnlyWorkspace,
} from './discovery-review-workspace.js';
export { computePromptFingerprint } from '../prompt.js';
export {
  buildPromptSizeTelemetry,
  setPromptSizeTelemetry,
} from '../telemetry.js';
