// Behavior tests for the canonical agent/guard-suppressions-pr-section.js
// renderer that the generated ./pr script invokes via base64-decoded
// `node /tmp/guard_suppressions_section.js`. We exercise the real file by
// spawning node — same code path as production.

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { execFileSync } from 'child_process';

const CANONICAL_SCRIPT = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  'guard-suppressions-pr-section.js',
);

let TMPDIR: string;
// The canonical script uses CommonJS `require`, but agent/package.json sets
// "type": "module" so direct invocation under that tree is interpreted as ESM
// and fails. Production never hits that path: the generated ./pr script writes
// the file to /tmp and runs `node /tmp/guard_suppressions_section_<taskId>.js`,
// where no parent package.json claims ESM and CJS is the default. Mirror that
// here by copying to TMPDIR before invocation.
let TMP_SCRIPT: string;

beforeAll(() => {
  TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), 'guard-suppressions-pr-section-test-'));
  TMP_SCRIPT = path.join(TMPDIR, 'guard-suppressions-pr-section.js');
  fs.copyFileSync(CANONICAL_SCRIPT, TMP_SCRIPT);
});

afterAll(() => {
  fs.rmSync(TMPDIR, { recursive: true, force: true });
});

function runScript(aggregate: unknown): string {
  let aggregateArg: string;
  if (aggregate === null) {
    aggregateArg = path.join(TMPDIR, 'does-not-exist.json');
  } else if (aggregate === '__malformed__') {
    aggregateArg = path.join(TMPDIR, `a-${Math.random().toString(36).slice(2)}.json`);
    fs.writeFileSync(aggregateArg, '{not valid json');
  } else {
    aggregateArg = path.join(TMPDIR, `a-${Math.random().toString(36).slice(2)}.json`);
    fs.writeFileSync(aggregateArg, JSON.stringify(aggregate));
  }
  return execFileSync('node', [TMP_SCRIPT, aggregateArg], { encoding: 'utf8' });
}

function bucket(entries: Record<string, { detail: string; reason?: string }> = {}, observed?: number) {
  const count = Object.keys(entries).length;
  return {
    entries,
    observed_count: observed === undefined ? count : observed,
    recorded_count: count,
    truncated: observed !== undefined && observed > count,
  };
}

function laneRecord(
  state: string,
  entries: Record<string, { detail: string; reason?: string }> = {},
  extra: { disappeared?: Record<string, { detail: string }>; skipped?: Record<string, { detail: string }> } = {},
) {
  return {
    version: 1,
    lane: 'lane',
    task_id: 'task',
    state,
    recorded_at: '2026-08-19T00:00:00+00:00',
    buckets: {
      suppressed: bucket(entries),
      disappeared: bucket(extra.disappeared ?? {}),
      skipped: bucket(extra.skipped ?? {}),
    },
  };
}

describe('guard-suppressions-pr-section.js renderer', () => {
  it('emits empty output when the aggregate file is missing', () => {
    expect(runScript(null)).toBe('');
  });

  it('emits empty output when JSON is malformed', () => {
    expect(runScript('__malformed__')).toBe('');
  });

  it('emits empty output when lanes is absent or empty', () => {
    expect(runScript({ version: 1, lanes: {} })).toBe('');
    expect(runScript({ version: 1 })).toBe('');
  });

  it('renders the marker-wrapped block listing state and suppressed identities per lane', () => {
    const out = runScript({
      version: 1,
      task_id: 't1',
      directory: '.',
      lanes: {
        static: laneRecord('passed'),
        vitest: laneRecord('preexisting_test_failure', {
          'vitest::flaky::test': { detail: 'AssertionError: inherited', reason: 'baseline' },
        }),
      },
      totals: {},
    });
    expect(out).toContain('<!-- blueprint:guard-suppressions -->');
    expect(out).toContain('<!-- /blueprint:guard-suppressions -->');
    expect(out).toContain('### Cross-lane guard suppressions');
    // Lane with no suppressions still surfaces its resolved state.
    expect(out).toContain('- lane `static`: state=passed');
    // Lane with a suppression surfaces the identity it relied on.
    expect(out).toContain('- lane `vitest`: state=preexisting_test_failure; suppressed: `vitest::flaky::test`');
  });

  it('renders disappeared and skipped identities, not just suppressed', () => {
    // The viewport lane's capture-skipped branch records its identity ONLY in
    // the skipped bucket — a suppressed-only renderer showed a bare state line.
    const out = runScript({
      lanes: {
        viewport: laneRecord('capture_skipped', {}, {
          skipped: { 'viewport::chromium_substrate_unavailable': { detail: 'no chromium' } },
        }),
        static: laneRecord('baseline_failure_disappeared', {}, {
          disappeared: { 'static::route::gone': { detail: 'vanished' } },
        }),
      },
    });
    expect(out).toContain(
      '- lane `viewport`: state=capture_skipped; skipped: `viewport::chromium_substrate_unavailable`',
    );
    expect(out).toContain(
      '- lane `static`: state=baseline_failure_disappeared; disappeared: `static::route::gone`',
    );
  });

  it('renders every non-empty bucket for one lane in suppressed/disappeared/skipped order', () => {
    const out = runScript({
      lanes: {
        static: laneRecord('preexisting_test_failure', { 'static::a': { detail: 'd' } }, {
          disappeared: { 'static::b': { detail: 'd' } },
          skipped: { 'static::c': { detail: 'd' } },
        }),
      },
    });
    expect(out).toContain(
      '- lane `static`: state=preexisting_test_failure; suppressed: `static::a`; disappeared: `static::b`; skipped: `static::c`',
    );
  });

  it('surfaces producer-side truncation in a non-suppressed bucket too', () => {
    const record = laneRecord('x', {}, { skipped: { 'lane::one': { detail: 'd' } } });
    record.buckets.skipped = bucket({ 'lane::one': { detail: 'd' } }, 40);
    const out = runScript({ lanes: { viewport: record } });
    expect(out).toContain('skipped: `lane::one` (+39 more)');
  });

  it('lists lanes in sorted order', () => {
    const out = runScript({
      lanes: { zeta: laneRecord('passed'), alpha: laneRecord('passed') },
    });
    expect(out.indexOf('lane `alpha`')).toBeLessThan(out.indexOf('lane `zeta`'));
  });

  it('caps identities shown per lane and notes the remainder', () => {
    const entries: Record<string, { detail: string }> = {};
    for (let i = 0; i < 15; i++) {
      entries[`id-${String(i).padStart(2, '0')}`] = { detail: `d${i}` };
    }
    const out = runScript({ lanes: { vitest: laneRecord('preexisting_test_failure', entries) } });
    expect(out).toContain('`id-00`');
    expect(out).toContain('`id-09`');
    expect(out).not.toContain('`id-10`');
    expect(out).toContain('(+5 more)');
  });

  it('caps the number of lanes shown and notes the remainder', () => {
    const lanes: Record<string, unknown> = {};
    for (let i = 0; i < 14; i++) {
      lanes[`lane-${String(i).padStart(2, '0')}`] = laneRecord('passed');
    }
    const out = runScript({ lanes });
    expect(out).toContain('lane `lane-00`');
    expect(out).toContain('lane `lane-11`');
    expect(out).not.toContain('lane `lane-12`');
    expect(out).toContain('2 more lane(s) not shown');
  });

  it('tolerates a lane record with a missing/invalid state or buckets shape', () => {
    const out = runScript({
      lanes: {
        weird: { state: 42, buckets: 'not-an-object' },
        ok: laneRecord('passed'),
      },
    });
    expect(out).toContain('- lane `ok`: state=passed');
    expect(out).toContain('- lane `weird`: state=unknown');
  });
});
