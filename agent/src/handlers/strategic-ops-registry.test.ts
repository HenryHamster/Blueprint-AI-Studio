import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { describe, expect, it } from 'vitest';

import {
  STRATEGIC_OPS_REGISTRY,
  cliOps,
  cliOpInput,
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
    // If this fails after a registry edit, regenerate the manifest:
    //   cd agent && npx tsx scripts/regen-strategic-ops-manifest.mjs
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

  it('teaches the complete direction/task structural contract', () => {
    const create = cliOpInput('create-direction');
    expect(create?.required).toEqual([
      'text',
      'success_evidence',
      'retirement_condition',
    ]);
    expect(create?.fields).toContain('staged');
    expect(create?.fields).toContain('gap_id');
    expect(create?.fields).toContain('cost_budget_usd');
    expect(create?.notes?.join('\n')).toContain('recurring autonomous research lane');
    expect(create?.notes?.join('\n')).toContain('multiple spawn-task calls under one direction');
    const update = cliOpInput('update-direction');
    expect(update?.fields).toContain('cost_budget_usd');
    expect(update?.notes?.join('\n')).toContain('set it to null to clear');
    const spawn = cliOpInput('spawn-task');
    expect(spawn?.example).toMatchObject({ direction_id: 'd-pipeline-reliability' });
    expect(spawn?.fields).toContain('depends_on');
    expect(spawn?.fields).toContain('require_deps_landed_on');
    expect(spawn?.fields).toContain('release_base_branch');
    expect(spawn?.fields).toContain('model');
    expect(spawn?.fields).toContain('effort');
    expect(spawn?.notes?.join('\n')).toContain('model and effort are an exact pair');
    expect(spawn?.notes?.join('\n')).toContain('never falls through');
    expect(spawn?.notes?.join('\n')).toContain(
      'bounded implementation, research, plan, design, and compute work under an existing strategic outcome',
    );
    // The ordered-workflow note teaches foundation-first sequencing plus the
    // named landing branch and the base re-cut.
    expect(spawn?.notes?.join('\n')).toContain('spawn the foundation first');
    expect(spawn?.notes?.join('\n')).toContain('require_deps_landed_on');
    expect(spawn?.notes?.join('\n')).toContain('release_base_branch');
    expect(spawn?.notes?.join('\n')).toContain('Keep genuinely independent work parallel');
    // The generated ./strategic schema spawn-task exposes the landing-gate fields.
    expect(script).toContain('"require_deps_landed_on"');
    expect(script).toContain('"release_base_branch"');
    expect(script).toContain('"required":["text","success_evidence","retirement_condition"]');
  });

  it('spawn-task advertises depends_on and documents its authoring-time rejection cases', () => {
    const spawn = cliOpInput('spawn-task');
    // The dependency field must be part of the advertised spawn schema so the
    // strategist can author layered work.
    expect(spawn?.fields).toContain('depends_on');
    // And the notes must teach the authoring-time rejection set (a bad edge is
    // a clear 400, never a silently dropped or wedged dependency).
    const notes = spawn?.notes?.join('\n') ?? '';
    expect(notes).toContain('depends_on entries are validated at authoring time');
    for (const word of ['cross-base', 'terminal-failed', 'cyclic', 'rejected']) {
      expect(notes, `spawn-task notes must mention the ${word} rejection case`).toContain(word);
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
