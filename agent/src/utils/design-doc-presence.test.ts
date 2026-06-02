/**
 * Tests for design-doc presence reconciliation.
 *
 * Verifies the three core behaviours required by the spec:
 *   (a) an entry whose referenced module files ALL exist is left UNCHANGED,
 *   (b) a claimed-present entry whose referenced module files are ALL absent
 *       is downgraded to `missing` with the correction visible,
 *   (c) an entry with NO module/file pointer is left untouched (absence of a
 *       path is not evidence of absent code).
 *
 * Uses a real temp directory so fs.existsSync is exercised end-to-end.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  reconcileEntry,
  reconcileSystemEntry,
  reconcileMissingCapabilities,
  reconcileDesignDocPresence,
  extractEntryPaths,
} from './design-doc-presence.js';

let root: string;

beforeAll(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'ddp-test-'));
  // Create one real backing file: backend/app/present.py
  fs.mkdirSync(path.join(root, 'backend', 'app'), { recursive: true });
  fs.writeFileSync(path.join(root, 'backend', 'app', 'present.py'), '# present\n');
});

afterAll(() => {
  fs.rmSync(root, { recursive: true, force: true });
});

describe('extractEntryPaths', () => {
  it('collects modules array and paths embedded in notes/description', () => {
    const paths = extractEntryPaths({
      modules: ['backend/app/a.py', 'agent/src/b.ts'],
      notes: 'see admin-ui/src/c.tsx for the widget',
      description: 'wires backend/app/a.py',
    });
    expect(paths).toContain('backend/app/a.py');
    expect(paths).toContain('agent/src/b.ts');
    expect(paths).toContain('admin-ui/src/c.tsx');
    // de-duped
    expect(paths.filter((p) => p === 'backend/app/a.py')).toHaveLength(1);
  });

  it('returns empty when there is no pointer', () => {
    expect(extractEntryPaths({ notes: 'no files here' })).toEqual([]);
  });
});

describe('reconcileSystemEntry', () => {
  it('(a) leaves a present system UNCHANGED when all module files exist', () => {
    const sys = {
      id: 'sys-x',
      name: 'X',
      status: 'functional',
      modules: ['backend/app/present.py'],
    };
    const out = reconcileSystemEntry(sys, root);
    expect(out).toBe(sys); // pure no-op returns the same reference
    expect(out?.status).toBe('functional');
  });

  it('(b) downgrades a present system to missing when all module files are absent', () => {
    const sys = {
      id: 'sys-y',
      name: 'Y',
      status: 'mature',
      description: 'core engine',
      modules: ['backend/app/gone.py', 'backend/app/also_gone.py'],
    };
    const out = reconcileSystemEntry(sys, root);
    expect(out?.status).toBe('missing');
    expect(out?.presence_warning).toMatch(/claimed mature/);
    expect(String(out?.description)).toMatch(/\[reconciled:/);
    // input not mutated
    expect(sys.status).toBe('mature');
  });

  it('does NOT downgrade when only some module files are absent', () => {
    const sys = {
      id: 'sys-z',
      name: 'Z',
      status: 'partial',
      modules: ['backend/app/present.py', 'backend/app/gone.py'],
    };
    const out = reconcileSystemEntry(sys, root);
    expect(out?.status).toBe('partial');
  });

  it('(c) leaves a present system with NO module pointer untouched', () => {
    const sys = { id: 'sys-w', name: 'W', status: 'functional' };
    const out = reconcileSystemEntry(sys, root);
    expect(out).toBe(sys);
    expect(out?.status).toBe('functional');
  });

  it('does NOT downgrade a non-present (stub/missing) system even with absent paths', () => {
    const sys = { id: 'sys-s', status: 'stub', modules: ['backend/app/gone.py'] };
    const out = reconcileSystemEntry(sys, root);
    expect(out?.status).toBe('stub');
  });

  it('is a conservative no-op when the root is not a valid directory', () => {
    const sys = { id: 'sys-y', status: 'mature', modules: ['backend/app/gone.py'] };
    const out = reconcileSystemEntry(sys, path.join(root, 'does-not-exist'));
    expect(out?.status).toBe('mature');
  });
});

describe('reconcileMissingCapabilities', () => {
  it('downgrades a working capability whose path (in notes) is absent', () => {
    const caps = {
      testing: { status: 'working', notes: 'implemented in backend/app/gone.py' },
    };
    const out = reconcileMissingCapabilities(caps, root);
    expect(out?.testing.status).toBe('missing');
    expect(out?.testing.notes).toMatch(/\[reconciled:/);
  });

  it('leaves a working capability whose path exists unchanged', () => {
    const caps = {
      testing: { status: 'working', notes: 'in backend/app/present.py' },
    };
    const out = reconcileMissingCapabilities(caps, root);
    expect(out?.testing.status).toBe('working');
  });

  it('leaves a capability with no pointer untouched', () => {
    const caps = { testing: { status: 'partial', notes: 'unit tests only' } };
    const out = reconcileMissingCapabilities(caps, root);
    expect(out?.testing.status).toBe('partial');
  });
});

describe('reconcileDesignDocPresence', () => {
  it('reconciles systems/* and capabilities/* keys and passes others through', () => {
    const doc = {
      summary: 'a project',
      'systems/sys-a': { id: 'sys-a', status: 'functional', modules: ['backend/app/gone.py'] },
      'systems/sys-b': { id: 'sys-b', status: 'functional', modules: ['backend/app/present.py'] },
      'capabilities/cap-a': { status: 'working', notes: 'backend/app/gone.py' },
      'notes/x': 'freeform',
      _meta: { revision: 3 },
    };
    const out = reconcileDesignDocPresence(doc, root) as Record<string, Record<string, unknown>>;
    expect(out['systems/sys-a'].status).toBe('missing');
    expect(out['systems/sys-b'].status).toBe('functional');
    expect(out['capabilities/cap-a'].status).toBe('missing');
    expect(out.summary).toBe('a project');
    expect(out._meta.revision).toBe(3);
    // original untouched
    expect((doc['systems/sys-a'] as Record<string, unknown>).status).toBe('functional');
  });

  it('is a conservative no-op when the root is invalid', () => {
    const doc = { 'systems/sys-a': { status: 'mature', modules: ['backend/app/gone.py'] } };
    const out = reconcileDesignDocPresence(doc, '/nonexistent/root');
    expect(out).toBe(doc);
  });
});

describe('reconcileEntry direct', () => {
  it('flips IMPROVE→missing only for present statuses', () => {
    const entry = { status: 'planned', notes: 'backend/app/gone.py' };
    // capability 'planned' is not a present status → untouched
    expect(reconcileEntry(entry, root, 'capability').status).toBe('planned');
  });
});
