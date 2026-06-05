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
  reconcileDesignDocIndex,
  extractEntryPaths,
} from './design-doc-presence.js';

let root: string;

beforeAll(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'ddp-test-'));
  // Create one real backing file: backend/app/present.py
  fs.mkdirSync(path.join(root, 'backend', 'app'), { recursive: true });
  fs.writeFileSync(path.join(root, 'backend', 'app', 'present.py'), '# present\n');
  // A file that exists and DEFINES `real_impl` (symbol present)...
  fs.writeFileSync(
    path.join(root, 'backend', 'app', 'impl.py'),
    'def real_impl():\n    return 1\n',
  );
  // ...and a re-export facade that exists but no longer defines `gone_func`.
  fs.writeFileSync(
    path.join(root, 'backend', 'app', 'facade.py'),
    'from .impl import real_impl  # facade shell, gone_func deleted\n',
  );
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

describe('reconcileDesignDocIndex', () => {
  it('downgrades a systems/* index entry whose modules are all absent', () => {
    const index = {
      sections: [
        { key: 'systems/sys-a', status: 'functional', modules: ['backend/app/gone.py'] },
        { key: 'capabilities/cap-a', status: 'working', notes: 'backend/app/gone.py' },
      ],
    };
    const out = reconcileDesignDocIndex(index, root) as { sections: Array<Record<string, unknown>> };
    expect(out.sections[0].status).toBe('missing');
    expect(out.sections[0].presence_warning).toMatch(/absent/);
    expect(out.sections[1].status).toBe('missing');
    // original untouched
    expect(index.sections[0].status).toBe('functional');
  });

  it('leaves present-backed entries and non-system/capability entries alone', () => {
    const index = {
      sections: [
        { key: 'systems/sys-b', status: 'functional', modules: ['backend/app/present.py'] },
        { key: 'notes/strategic-context', summary: 'v5 stabilize' },
      ],
    };
    const out = reconcileDesignDocIndex(index, root) as { sections: Array<Record<string, unknown>> };
    expect(out.sections[0].status).toBe('functional');
    expect(out.sections[1].summary).toBe('v5 stabilize');
  });

  it('no-ops on missing/non-array sections and an invalid root', () => {
    expect(reconcileDesignDocIndex(undefined, root)).toBeUndefined();
    const noSections = { foo: 'bar' };
    expect(reconcileDesignDocIndex(noSections, root)).toBe(noSections);
    const nonArray = { sections: 'nope' as unknown };
    expect(reconcileDesignDocIndex(nonArray, root)).toBe(nonArray);
    const validIndex = { sections: [{ key: 'systems/x', status: 'mature', modules: ['backend/app/gone.py'] }] };
    expect(reconcileDesignDocIndex(validIndex, '/nonexistent/root')).toBe(validIndex);
  });
});

describe('reconcileEntry direct', () => {
  it('flips IMPROVE→missing only for present statuses', () => {
    const entry = { status: 'planned', notes: 'backend/app/gone.py' };
    // capability 'planned' is not a present status → untouched
    expect(reconcileEntry(entry, root, 'capability').status).toBe('planned');
  });
});

describe('symbol-level grounding (present file + absent symbol)', () => {
  it('present file + present symbol → NOT flagged', () => {
    const sys = {
      id: 'sys-sym-ok',
      status: 'functional',
      modules: ['backend/app/impl.py'],
      description: 'core logic in backend/app/impl.py:real_impl',
    };
    const out = reconcileEntry(sys, root, 'system');
    expect(out).toBe(sys); // pure no-op
    expect(out.status).toBe('functional');
  });

  it('present file + absent symbol → flagged as missing', () => {
    const sys = {
      id: 'sys-sym-gone',
      status: 'mature',
      // facade.py exists, so the file-level check passes — but gone_func is gone.
      modules: ['backend/app/facade.py'],
      description: 'behavior lives in backend/app/facade.py:gone_func',
    };
    const out = reconcileEntry(sys, root, 'system');
    expect(out.status).toBe('missing');
    expect(out.presence_warning).toMatch(/symbol `backend\/app\/facade\.py:gone_func` is absent/);
    expect(String(out.description)).toMatch(/\[reconciled:/);
    // input not mutated
    expect(sys.status).toBe('mature');
  });

  it('symbol cited in a capability notes field is flagged too', () => {
    const caps = {
      retarget: { status: 'working', notes: 'see backend/app/facade.py:gone_func' },
    };
    const out = reconcileMissingCapabilities(caps, root);
    expect(out?.retarget.status).toBe('missing');
  });

  it('indeterminate (cited file does not exist) is NOT symbol-flagged', () => {
    // No file at all → the file-level path check owns this case, and the
    // symbol read is indeterminate, so it must not produce a symbol-flag.
    const sys = {
      id: 'sys-sym-indet',
      status: 'functional',
      // present.py exists so the file-level check does NOT downgrade (partial hit),
      // and the symbol ref points at an absent file → indeterminate, not flagged.
      modules: ['backend/app/present.py'],
      description: 'helper in backend/app/never_existed.py:phantom',
    };
    const out = reconcileEntry(sys, root, 'system');
    expect(out).toBe(sys);
    expect(out.status).toBe('functional');
  });
});
