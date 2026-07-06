import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { describe, expect, it } from 'vitest';

import {
  STRATEGIC_OPS_REGISTRY,
  cliOps,
  promptOps,
  allBackendOps,
  strategicOpsManifest,
} from './strategic-ops-registry.js';
import { writeStrategicHelpers } from './strategic-helpers.js';
import { buildBootstrapPrompt } from './strategic-resident-lib.js';

// The single canonical registry (strategic-ops-registry.ts) is the source of
// truth for the strategist mutating-op surface. These drift-guards fail if the
// generated ./strategic CLI, the resident bootstrap prompt, or the checked-in
// shared manifest list a different op set than the registry — so the three
// surfaces (four counting the backend, guarded by test_strategic_ops_registry.py)
// can never silently diverge again.

const MANIFEST_PATH = path.resolve(
  process.cwd(),
  '..',
  'backend/app/strategic_ops_manifest.json',
);

function renderStrategicScript(): string {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'strategic-ops-registry-'));
  try {
    writeStrategicHelpers('task-drift', 'proj-drift', ws);
    return fs.readFileSync(path.join(ws, 'strategic'), 'utf8');
  } finally {
    try { fs.rmSync(ws, { recursive: true }); } catch { /* ignore */ }
  }
}

function renderBootstrap(): string {
  return buildBootstrapPrompt({
    contextPrefix: '',
    projectName: 'Drift',
    problemModelJson: '{}',
    runningUpdate: '',
    directionsText: '',
    stagedDirectionsText: '',
    retiredDirectionsText: '',
    previousDecisionOutcomesText: '',
    visionJson: '',
    openMessagesText: '',
    sessionId: 'ss-drift',
  });
}

describe('strategic ops registry — internal consistency', () => {
  it('backend op names are unique and non-empty', () => {
    const ops = allBackendOps();
    expect(ops.length).toBe(STRATEGIC_OPS_REGISTRY.length);
    expect(new Set(ops).size).toBe(ops.length);
    for (const op of ops) expect(op).toMatch(/^[a-z][a-z_]*$/);
  });

  it('every CLI-flagged op has a kebab alias; internal-only ops have none and a reason', () => {
    for (const e of STRATEGIC_OPS_REGISTRY) {
      if (e.cli || e.prompt) {
        expect(e.alias, `${e.op} is surfaced but has no alias`).toBeTruthy();
        expect(e.alias).toMatch(/^[a-z][a-z-]*$/);
        // The CLI alias must be the kebab-case of the backend op name so the
        // generated `<alias>) _call "<op>"` arm routes correctly.
        expect(e.alias).toBe(e.op.replace(/_/g, '-'));
        expect(e.internalReason).toBeNull();
      } else {
        // Excluded EXPLICITLY: no alias, and a stated reason (not by omission).
        expect(e.alias, `${e.op} is internal-only but carries an alias`).toBeNull();
        expect(e.internalReason, `${e.op} must state why it is excluded`).toBeTruthy();
      }
    }
  });

  it('CLI aliases are unique', () => {
    const aliases = cliOps().map(e => e.alias);
    expect(new Set(aliases).size).toBe(aliases.length);
  });
});

describe('shared manifest lockstep', () => {
  it('backend/app/strategic_ops_manifest.json equals the registry projection', () => {
    const onDisk: unknown = JSON.parse(readFileSync(MANIFEST_PATH, 'utf8'));
    // If this fails after a registry edit, regenerate the manifest from
    // strategicOpsManifest() (see strategic-ops-registry.ts header).
    expect(onDisk).toEqual(strategicOpsManifest());
  });
});

describe('generated ./strategic CLI derives from the registry', () => {
  const script = renderStrategicScript();

  it('usage string lists exactly the CLI-flagged aliases in registry order', () => {
    const expected = `Usage: strategic ${cliOps().map(e => e.alias).join('|')} <json-or-file>`;
    expect(script).toContain(expected);
  });

  it('every CLI-flagged op has a case arm routing its alias to _call "<op>"', () => {
    for (const e of cliOps()) {
      expect(script, `missing case arm for ${e.alias}`).toContain(`${e.alias})`);
      expect(script, `missing dispatch for ${e.op}`).toContain(`_call "${e.op}"`);
    }
  });

  it('no internal-only op leaks onto the CLI (case arm or _call dispatch)', () => {
    for (const e of STRATEGIC_OPS_REGISTRY.filter(x => !x.cli)) {
      const alias = e.op.replace(/_/g, '-');
      expect(script, `internal op ${e.op} leaked a case arm`).not.toContain(`${alias})`);
      expect(script, `internal op ${e.op} leaked a dispatch`).not.toContain(`_call "${e.op}"`);
    }
  });

  it('the count of _call dispatch arms matches the CLI-flagged op count', () => {
    const calls = [...script.matchAll(/_call "([a-z_]+)"/g)].map(m => m[1]);
    expect(new Set(calls)).toEqual(new Set(cliOps().map(e => e.op)));
  });
});

describe('resident bootstrap prompt derives from the registry', () => {
  const prompt = renderBootstrap();

  it('lists exactly the prompt-flagged aliases and drops the misleading ellipsis', () => {
    const expectedList = promptOps().map(e => e.alias).join(', ');
    expect(prompt).toContain(`apply strategic operations (${expectedList})`);
    // The old hand-typed list ended in an ellipsis implying "and more" — gone.
    expect(prompt).not.toContain('pending-confirm, …');
    expect(prompt).not.toContain('…');
  });

  it('does not advertise any internal-only op alias', () => {
    for (const e of STRATEGIC_OPS_REGISTRY.filter(x => !x.prompt)) {
      const alias = e.op.replace(/_/g, '-');
      expect(prompt, `internal op ${e.op} leaked into the prompt`).not.toContain(alias);
    }
  });
});
