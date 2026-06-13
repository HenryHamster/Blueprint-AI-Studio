// Behavior tests for the canonical agent/screenshots-pr-section.js renderer that
// the generated ./pr script invokes via base64-decoded `node /tmp/screenshots_section.js`.
// We exercise the real file by spawning node — same code path as production.

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { execFileSync } from 'child_process';

const CANONICAL_SCRIPT = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  'screenshots-pr-section.js',
);

let TMPDIR: string;
// The canonical script uses CommonJS `require`, but agent/package.json sets
// "type": "module" so direct invocation under that tree is interpreted as ESM
// and fails. Production never hits that path: the generated ./pr script writes
// the file to /tmp and runs `node /tmp/screenshots_section_<taskId>.js`, where
// no parent package.json claims ESM and CJS is the default. Mirror that here by
// copying to TMPDIR before invocation.
let TMP_SCRIPT: string;

const ORCH = 'https://orchestrator.example';
const PROJECT = 'proj-abc';
const TASK = 'task-xyz';

beforeAll(() => {
  TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), 'screenshots-pr-section-test-'));
  TMP_SCRIPT = path.join(TMPDIR, 'screenshots-pr-section.js');
  fs.copyFileSync(CANONICAL_SCRIPT, TMP_SCRIPT);
});

afterAll(() => {
  fs.rmSync(TMPDIR, { recursive: true, force: true });
});

function runScript(
  manifest: unknown,
  opts: { orch?: string; project?: string; task?: string } = {},
): string {
  let manifestArg: string;
  if (manifest === null) {
    manifestArg = path.join(TMPDIR, 'does-not-exist.json');
  } else if (manifest === '__malformed__') {
    manifestArg = path.join(TMPDIR, `m-${Math.random().toString(36).slice(2)}.json`);
    fs.writeFileSync(manifestArg, '{not valid json');
  } else {
    manifestArg = path.join(TMPDIR, `m-${Math.random().toString(36).slice(2)}.json`);
    fs.writeFileSync(manifestArg, JSON.stringify(manifest));
  }
  const args = [
    TMP_SCRIPT,
    manifestArg,
    opts.orch ?? ORCH,
    opts.project ?? PROJECT,
    opts.task ?? TASK,
  ];
  return execFileSync('node', args, { encoding: 'utf8' });
}

function changedPair(over: Record<string, unknown> = {}) {
  return {
    test_file: 'TaskCard.viewport.test.tsx',
    test: 'taskcard-default',
    width: 375,
    before: '/tmp/b/a.png',
    after: '/tmp/a/a.png',
    status: 'changed',
    ...over,
  };
}

describe('screenshots-pr-section.js renderer', () => {
  it('emits empty output when the manifest file is missing', () => {
    expect(runScript(null)).toBe('');
  });

  it('emits empty output when JSON is malformed', () => {
    expect(runScript('__malformed__')).toBe('');
  });

  it('emits empty output when there are no pairs', () => {
    expect(runScript({ pairs: [], summary: {} })).toBe('');
  });

  it('emits empty output when only unchanged/removed pairs exist', () => {
    const out = runScript({
      pairs: [
        { test: 'a', width: 375, status: 'unchanged', before: '/b', after: '/a' },
        { test: 'b', width: 375, status: 'removed', before: '/b', after: null },
      ],
      summary: { unchanged: 1, removed: 1 },
    });
    expect(out).toBe('');
  });

  it('renders the marker-wrapped block with header and table for changed+new', () => {
    const out = runScript({
      pairs: [
        changedPair(),
        { test: 'newcomp-empty', width: 768, status: 'new', before: null, after: '/tmp/a/b.png' },
        { test: 'unch', width: 375, status: 'unchanged', before: '/b', after: '/a' },
      ],
      summary: { changed: 1, new: 1, unchanged: 1, removed: 0 },
    });
    expect(out).toContain('<!-- blueprint:screenshots -->');
    expect(out).toContain('<!-- /blueprint:screenshots -->');
    expect(out).toContain('## Viewport Screenshots');
    expect(out).toContain('1 changed, 1 new, 1 unchanged');
    expect(out).toContain('| Component | Width | Status | Before | After |');
    // Changed row carries both before and after links.
    expect(out).toContain('| taskcard-default | 375 | changed |');
    // New row omits the before link (empty cell) but keeps after.
    expect(out).toMatch(/\| newcomp-empty \| 768 \| new \|\s*\|/);
  });

  it('builds download links whose name reconstructs the uploaded object path', () => {
    const out = runScript({
      pairs: [changedPair()],
      summary: { changed: 1 },
    });
    // name = ${test}-${width}-${phase}; the download endpoint re-sanitizes it to
    // the exact object the upload endpoint stored (sanitize(name)-width-phase.png).
    expect(out).toContain(
      `${ORCH}/api/admin/viewport-shots/download-url?project_id=${PROJECT}&task_id=${TASK}&name=taskcard-default-375-before`,
    );
    expect(out).toContain('&name=taskcard-default-375-after');
  });

  it('url-encodes project, task, and name query params', () => {
    const out = runScript(
      { pairs: [changedPair({ test: 'has space/slash' })], summary: { changed: 1 } },
      { project: 'p/1', task: 't 2' },
    );
    expect(out).toContain('project_id=p%2F1');
    expect(out).toContain('task_id=t%202');
    expect(out).toContain('name=has%20space%2Fslash-375-before');
  });

  it('strips a trailing slash from the orchestrator URL', () => {
    const out = runScript({ pairs: [changedPair()], summary: { changed: 1 } }, { orch: 'https://x.example/' });
    expect(out).toContain('https://x.example/api/admin/viewport-shots/download-url');
    expect(out).not.toContain('https://x.example//api');
  });

  it('caps the table at 12 changed/new pairs and notes the truncation', () => {
    const pairs = Array.from({ length: 15 }, (_, i) => ({
      test: `comp-${i}`,
      width: 375,
      status: 'changed',
      before: `/b/${i}.png`,
      after: `/a/${i}.png`,
    }));
    const out = runScript({ pairs, summary: { changed: 15 } });
    // 12 rendered rows + the two header rows.
    expect(out).toContain('| comp-0 | 375 | changed |');
    expect(out).toContain('| comp-11 | 375 | changed |');
    expect(out).not.toContain('| comp-12 |');
    expect(out).toContain('+3 more changed/new pair(s) not shown (capped at 12)');
  });

  it('escapes pipe characters in the component name so the table stays valid', () => {
    const out = runScript({
      pairs: [changedPair({ test: 'a|b' })],
      summary: { changed: 1 },
    });
    expect(out).toContain('| a\\|b | 375 | changed |');
  });
});
