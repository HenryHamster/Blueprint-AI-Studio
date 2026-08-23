import { describe, expect, it } from 'vitest';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// The merge-time verifier runs from a baked agent image containing agent/src,
// not the backend checkout. Keep the packaged executable byte-identical to the
// backend's canonical attributor so Node invokes the same route/detail rules
// as the publication gate instead of maintaining a second comparator.
const here = path.dirname(fileURLToPath(import.meta.url));
const agentCopy = path.join(here, 'fixgate_attribution.py');
const backendCanonical = path.resolve(
  here,
  '../../../backend/testing/fixgate_attribution.py',
);

describe('fixgate_attribution.py agent copy parity', () => {
  it('is byte-identical to the backend canonical (single source of truth)', () => {
    const agent = fs.readFileSync(agentCopy);
    let backend: Buffer | null = null;
    try {
      backend = fs.readFileSync(backendCanonical);
    } catch {
      // Isolated agent checkouts do not carry backend/, so parity can only be
      // verified in a monorepo checkout where both artifacts are available.
      backend = null;
    }
    if (backend === null) return;
    expect(agent.equals(backend)).toBe(true);
  });
});
