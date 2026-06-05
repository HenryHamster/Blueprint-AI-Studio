// Shared bake-smoke stand-in for the published task-capability contract.
//
// The real agent loads the backend contract (GET /api/agent/task-capabilities)
// once per VM at startup, BEFORE any handler dispatch — see
// ensureTaskCapabilityContractLoaded in src/index.ts. The per-handler bake
// smokes invoke handlers.memo() directly and skip that startup step, so any
// smoke whose payload reaches configureAgentPermissions must load an
// equivalent contract first; classifying without one throws (fail explicit,
// by design — a missing contract must never silently grant write permissions).
//
// The map is generated from AGENT_TASK_TYPE_IDS so it always satisfies the
// loader's completeness check, including for task types added later. Every
// entry is read_only: that is the fail-safe mode for a stub, and memo — the
// only type any bake smoke actually dispatches — is read_only in the
// production registry too. If a future smoke dispatches a write-mode type,
// give that id its real production mode here rather than working around it
// in the smoke.

import {
  AGENT_TASK_TYPE_IDS,
  loadTaskCapabilityContract,
  type TaskCapabilityContract,
} from '../taskCapabilities.js';

export function buildBakeSmokeCapabilityContract(): TaskCapabilityContract {
  return Object.fromEntries(
    AGENT_TASK_TYPE_IDS.map((id) => [id, { agent_mode: 'read_only' }]),
  );
}

export function loadBakeSmokeCapabilityContract(): void {
  loadTaskCapabilityContract(buildBakeSmokeCapabilityContract());
}
