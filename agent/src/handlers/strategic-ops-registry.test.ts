import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { describe, expect, it } from 'vitest';

import {
  STRATEGIC_OPS_REGISTRY,
  cliOps,
  allBackendOps,
  strategicOpsManifest,
} from './strategic-ops-registry.js';
import { writeStrategicHelpers } from './strategic-helpers.js';

// The single canonical registry (strategic-ops-registry.ts) is the source of
// truth for the strategist mutating-op surface. These drift-guards fail if the
// generated ./strategic CLI or the checked-in
// shared manifest list a different op set than the registry — so the
// surfaces (plus the backend, guarded by test_strategic_ops_registry.py)
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
    expect(script).toContain("Usage: strategic <operation> (--json '<object>' | --file <path> | --stdin)");
    for (const entry of cliOps()) {
      expect(script).toContain(`echo '${entry.alias}\t`);
    }
  });

  it('provides offline help and a machine-readable schema for every CLI operation', () => {
    expect(script).toContain('strategic help <operation>');
    expect(script).toContain('strategic schema <operation>');
    for (const entry of cliOps()) {
      expect(script).toContain(`operation":"${entry.alias}`);
    }
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

describe('API reference docs derive from the registry', () => {
  const API_REFERENCE_PATH = path.resolve(
    process.cwd(),
    '..',
    'docs/API_REFERENCE.md',
  );
  const apiDoc = readFileSync(API_REFERENCE_PATH, 'utf8');

  // Extract the op union string from the POST /api/agent/strategic request body
  // example. Scoped to the `/api/agent/strategic` section to avoid matching op
  // fields in other endpoint examples. The format is: "op": "<op1> | <op2> | ..."
  function extractDocumentedOps(doc: string): Set<string> {
    const sectionMatch = doc.match(/POST \/api\/agent\/strategic[\s\S]*?"op":\s*"([^"]+)"/);
    if (!sectionMatch) return new Set();
    return new Set(sectionMatch[1].split('|').map(s => s.trim()).filter(Boolean));
  }

  const documentedOps = extractDocumentedOps(apiDoc);

  it('documents every non-internal op in the op union string', () => {
    const surfacedOps = STRATEGIC_OPS_REGISTRY.filter(e => e.cli || e.prompt);
    for (const e of surfacedOps) {
      expect(
        documentedOps,
        `API_REFERENCE.md op union missing non-internal op: ${e.op}`,
      ).toContain(e.op);
    }
  });

  it('does not include internal-only ops in the op union string', () => {
    for (const e of STRATEGIC_OPS_REGISTRY.filter(x => !x.cli && !x.prompt)) {
      expect(
        documentedOps,
        `API_REFERENCE.md op union should not list internal op: ${e.op}`,
      ).not.toContain(e.op);
    }
  });
});
