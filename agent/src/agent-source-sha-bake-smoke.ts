// Bake-time smoke: exercise the production readAgentSourceSha() reader against
// the /opt/agent/AGENT_SOURCE_SHA marker written by infra/packer/agent.pkr.hcl
// so a drift between the write format and the read regex fails the bake before
// the image is tagged. Mirrors fingerprint-bake-smoke.ts. Invoked only via
// `node --import tsx ./src/agent-source-sha-bake-smoke.ts`; guarded so an
// accidental import does not trigger a real fs read in tests.
//
// Unlike the fingerprint marker (always a real content hash), the source SHA may
// legitimately be the baked 'unknown' fallback on a build with no commit in the
// environment (e.g. a local/dev bake without GITHUB_SHA). In that case the bake
// must still succeed, so the smoke passes when the marker carries an empty or
// 'unknown' value and only asserts a clean round-trip when a real SHA was baked.
import fs from 'fs';
import { readAgentSourceSha } from './index.js';

function runSmoke(): void {
  let raw: string;
  try {
    raw = fs.readFileSync('/opt/agent/AGENT_SOURCE_SHA', 'utf-8').trim();
  } catch {
    throw new Error(
      '/opt/agent/AGENT_SOURCE_SHA is missing — the packer write step did not run. ' +
        'Restore the AGENT_SOURCE_SHA write line in infra/packer/agent.pkr.hcl.',
    );
  }
  const m = /^AGENT_SOURCE_SHA=(.*)$/.exec(raw);
  if (!m) {
    throw new Error(
      `/opt/agent/AGENT_SOURCE_SHA has an unexpected format: ${JSON.stringify(raw)}. ` +
        'Expected a single line `AGENT_SOURCE_SHA=<sha|unknown>`.',
    );
  }
  const baked = m[1];
  // The reader prefers an AGENT_SOURCE_SHA env var (runner-provided host-checkout
  // provenance). A bake host that happens to inherit one must not let the smoke
  // pass off a runner override as proof of the baked marker — clear it so the
  // smoke exercises the marker fallback this image actually carries.
  delete process.env.AGENT_SOURCE_SHA;
  const parsed = readAgentSourceSha();
  if (baked === '' || baked === 'unknown') {
    // Acceptable dev/local fallback — reader must report null, never a bogus SHA.
    if (parsed !== null) {
      throw new Error(
        `readAgentSourceSha() returned ${JSON.stringify(parsed)} for an empty/unknown ` +
          'marker — it must return null so no bogus SHA is sent over the wire.',
      );
    }
    return;
  }
  // A real SHA was baked: the production reader MUST parse it back identically,
  // otherwise the packer write line and the agent regex have drifted.
  if (parsed === null) {
    throw new Error(
      `readAgentSourceSha() returned null but the marker carries a real value ` +
        `(${JSON.stringify(baked)}). The write line in agent.pkr.hcl and the ` +
        '/^AGENT_SOURCE_SHA=([0-9a-f]{40})$/ regex in agent/src/index.ts have drifted.',
    );
  }
  if (parsed !== baked) {
    throw new Error(
      `readAgentSourceSha() returned ${JSON.stringify(parsed)} but the marker carries ` +
        `${JSON.stringify(baked)} — round-trip mismatch.`,
    );
  }
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  runSmoke();
}
