// Global vitest setup (registered via vitest.config.ts `setupFiles`).
//
// The agent's read_only/write task partition is now sourced at runtime from the
// published backend capability contract, and isReadOnlyTaskType/
// configureAgentPermissions THROW until that contract is loaded. Many tests
// exercise those code paths indirectly (handler smoke dispatch, prompt
// permission tests). Seed a canonical fixture contract before every test file so
// those paths resolve against a complete, realistic contract instead of throwing.
//
// Tests that specifically exercise the load/validation flow reset the cache via
// __resetTaskCapabilityContractForTesting() and re-seed as needed.
//
// The contract is loaded at module top-level (not only in a hook) because some
// suites call getReadOnlyTaskTypes()/getWriteTaskTypes() inside `it.each(...)`,
// which evaluates during test collection — before any beforeAll hook runs.
// setupFiles execute before the test module is collected, so a top-level load
// guarantees the partition exists by collection time. Vitest isolates each test
// file's module graph, so this runs fresh per file.
import { beforeAll } from 'vitest';
import {
  AGENT_TASK_TYPE_IDS,
  loadTaskCapabilityContract,
  type TaskCapabilityContract,
} from './taskCapabilities.js';

// Mirrors the backend agent_mode assignments in
// backend/app/task_capabilities.py (TASK_CAPABILITIES). Kept here as a test
// fixture only — production sources this from the live endpoint.
const FIXTURE_READ_ONLY: ReadonlySet<string> = new Set([
  'review',
  'panel_review',
  'plan',
  'research',
  'strategic_review',
  'design_doc_update',
  'security_audit',
  'smoketest',
  'debug',
  'perf',
  'investigate',
  'self_audit',
  'release_audit',
  'memo',
  'verify_tests',
  'compute_experiment',
  'web_research',
  'design',
]);

/** Build a complete, valid capability contract covering every agent task type. */
export function buildFixtureCapabilityContract(): TaskCapabilityContract {
  const contract: TaskCapabilityContract = {};
  for (const id of AGENT_TASK_TYPE_IDS) {
    contract[id] = {
      type: id,
      label: id,
      agent_mode: FIXTURE_READ_ONLY.has(id) ? 'read_only' : 'write',
    };
  }
  return contract;
}

// Seed at collection time (top-level) so it.each(...) over the partition works.
loadTaskCapabilityContract(buildFixtureCapabilityContract());

// Re-seed before the suite proper, in case a top-of-file test reset the cache.
beforeAll(() => {
  loadTaskCapabilityContract(buildFixtureCapabilityContract());
});
