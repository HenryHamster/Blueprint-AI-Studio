import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// The agent ships a byte-identical copy of the backend's canonical
// materialize_manifest.py so the warm re-target path can re-materialize a new
// account's credentials using the SAME helper the boot path uses (the backend
// tree is not present on the agent VM image; infra/packer copies agent/src →
// /opt/agent/src). This is NOT a parallel materializer — it is one logical
// source enforced here. If you edit one file, edit both; CI fails otherwise.
const here = path.dirname(fileURLToPath(import.meta.url));
const agentCopy = path.join(here, 'materialize_manifest.py');
// here = agent/src/_agent_scripts → up three to repo root.
const backendCanonical = path.resolve(
  here,
  '../../../backend/app/_agent_scripts/materialize_manifest.py',
);

describe('materialize_manifest.py agent copy parity', () => {
  it('is byte-identical to the backend canonical (single source of truth)', () => {
    const agent = fs.readFileSync(agentCopy);
    let backend: Buffer | null = null;
    try {
      backend = fs.readFileSync(backendCanonical);
    } catch {
      // Backend tree absent (isolated agent checkout). The drift guard only
      // applies when both files are present (the monorepo CI case).
      backend = null;
    }
    if (backend === null) return;
    expect(agent.equals(backend)).toBe(true);
  });
});
