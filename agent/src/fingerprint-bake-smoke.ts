// Bake-time smoke: exercise the production readAgentRuntimeFingerprint()
// reader against the file written by infra/packer/agent.pkr.hcl so a drift
// between write format and read regex fails the bake before the image is
// tagged. Invoked only via `node --import tsx ./src/fingerprint-bake-smoke.ts`;
// guarded so accidental imports do not trigger a real fs read in tests.
import { readAgentRuntimeFingerprint } from './index.js';

function runSmoke(): void {
  const value = readAgentRuntimeFingerprint();
  if (value === null) {
    throw new Error(
      'readAgentRuntimeFingerprint() returned null — write step missing or unreadable, ' +
        'or the line in /opt/agent/AGENT_RUNTIME_FINGERPRINT does not match ' +
        '/^AGENT_RUNTIME_FINGERPRINT=([0-9a-f]{64})$/. Inspect the file with ' +
        '`sudo cat /opt/agent/AGENT_RUNTIME_FINGERPRINT | od -c | head` to diagnose.',
    );
  }
  if (!/^[0-9a-f]{64}$/.test(value)) {
    throw new Error(
      `readAgentRuntimeFingerprint() returned a non-64-hex value (length=${value.length}): ` +
        JSON.stringify(value),
    );
  }
}

if (process.argv[1] && import.meta.url === `file://${process.argv[1]}`) {
  runSmoke();
}
