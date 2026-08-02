// Behavior tests for the canonical agent/sr-pr-section.js renderer that the
// generated ./pr script invokes via base64-decoded `node /tmp/sr_section.js`.
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
  'sr-pr-section.js',
);

let TMPDIR: string;
// The canonical script uses CommonJS `require`, but agent/package.json sets
// "type": "module" so direct invocation under that tree is interpreted as
// ESM and fails. Production never hits that path: the generated ./pr script
// writes the file to /tmp and runs `node /tmp/sr_section_<taskId>.js`, where
// no parent package.json claims ESM and CJS is the default. Mirror that here
// by copying to TMPDIR before each invocation.
let TMP_SCRIPT: string;

beforeAll(() => {
  TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), 'sr-pr-section-test-'));
  TMP_SCRIPT = path.join(TMPDIR, 'sr-pr-section.js');
  fs.copyFileSync(CANONICAL_SCRIPT, TMP_SCRIPT);
});

afterAll(() => {
  fs.rmSync(TMPDIR, { recursive: true, force: true });
});

function runScript(srJson: unknown, skipReason?: string): string {
  const args: string[] = [TMP_SCRIPT];
  if (srJson === null) {
    args.push(path.join(TMPDIR, 'does-not-exist.json'));
  } else {
    const srPath = path.join(TMPDIR, `sr-${Math.random().toString(36).slice(2)}.json`);
    fs.writeFileSync(srPath, JSON.stringify(srJson));
    args.push(srPath);
  }
  if (skipReason !== undefined) {
    const skipPath = path.join(TMPDIR, `skip-${Math.random().toString(36).slice(2)}.txt`);
    fs.writeFileSync(skipPath, skipReason);
    args.push(skipPath);
  }
  const out = execFileSync('node', args, { encoding: 'utf8' });
  return out;
}

describe('sr-pr-section.js renderer', () => {
  it('emits empty output when sr file is missing', () => {
    expect(runScript(null)).toBe('');
  });

  it('renders pass verdict with checkmark and prompt_version', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      prompt_version: 'v2',
      check_results: {},
    });
    expect(out).toContain('<!-- blueprint:self-review -->');
    expect(out).toContain('<!-- /blueprint:self-review -->');
    expect(out).toContain('pass ✓');
    expect(out).toContain('fixes_applied: no');
    expect(out).toContain('**prompt_version:** v2');
    expect(out).not.toContain('Failing categories');
  });

  it('renders fixes_applied: yes', () => {
    const out = runScript({ verdict: 'pass', fixes_applied: true });
    expect(out).toContain('fixes_applied: yes');
  });

  it('renders fail verdict with cross mark', () => {
    const out = runScript({ verdict: 'fail', fixes_applied: false });
    expect(out).toContain('fail ✗');
  });

  it('lists failing categories from check_results', () => {
    const out = runScript({
      verdict: 'fail',
      fixes_applied: false,
      check_results: {
        correctness: { passed: false },
        security: { passed: true },
        'type-safety': { passed: false },
      },
    });
    expect(out).toContain('Failing categories (2):');
    expect(out).toContain('correctness');
    expect(out).toContain('type-safety');
    expect(out).not.toMatch(/security[,\s]/);
  });

  it('clamps long failing-category list with +K more', () => {
    const check_results = Object.fromEntries(
      ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map(k => [k, { passed: false }]),
    );
    const out = runScript({ verdict: 'fail', fixes_applied: false, check_results });
    expect(out).toContain('+2 more');
    expect(out).toContain('Failing categories (7):');
  });

  it('renders skip reason from external file argument when verdict is unknown', () => {
    const out = runScript({ verdict: 'unknown' }, 'session_timeout');
    expect(out).toContain('Self-review skipped — reason: session_timeout');
  });

  it('falls back to sr.skip_reason when skip-file is absent', () => {
    const out = runScript({ verdict: 'unknown', skip_reason: 'json_missing' });
    expect(out).toContain('Self-review skipped — reason: json_missing');
  });

  it('falls back to "unknown" when neither skip-file nor skip_reason are present', () => {
    const out = runScript({ verdict: 'unknown' });
    expect(out).toContain('Self-review skipped — reason: unknown');
  });

  it('renders edges_considered count for handled edges', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      edges_considered: [
        { case: 'empty input', handled: true, evidence: 'early return' },
        { case: 'null refs', handled: true, evidence: 'null-checks' },
      ],
    });
    expect(out).toContain('Edges considered (handled): 2');
  });

  it('lists unhandled edges with blind_spot under distinct header', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      edges_considered: [
        { case: 'concurrent calls', handled: false, blind_spot: 'no lock mechanism' },
        { case: 'overflow on large input', handled: false, blind_spot: 'buffer size not validated' },
      ],
    });
    expect(out).toContain('⚠ Unhandled edges (author-flagged):');
    expect(out).toContain('concurrent calls — no lock mechanism');
    expect(out).toContain('overflow on large input — buffer size not validated');
  });

  it('truncates unhandled edges list with +N more when exceeding cap', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      edges_considered: [
        { case: 'edge1', handled: false, blind_spot: 'issue1' },
        { case: 'edge2', handled: false, blind_spot: 'issue2' },
        { case: 'edge3', handled: false, blind_spot: 'issue3' },
        { case: 'edge4', handled: false, blind_spot: 'issue4' },
      ],
    });
    expect(out).toContain('⚠ Unhandled edges (author-flagged):');
    expect(out).toContain('edge1');
    expect(out).toContain('+1 more');
    expect(out).not.toContain('edge4');
  });

  it('shows files not re-verified when incomplete_review and truncated_files_read present', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      categories: ['incomplete_review'],
      truncated_files_read: ['agent/src/foo.ts', 'backend/app/bar.py'],
    });
    expect(out).toContain('Files not re-verified:');
    expect(out).toContain('agent/src/foo.ts');
    expect(out).toContain('backend/app/bar.py');
  });

  it('clamps output to 1500 chars and preserves closing marker', () => {
    const out = runScript({
      verdict: 'fail',
      fixes_applied: false,
      check_results: Object.fromEntries(
        Array.from({ length: 20 }, (_, i) => [`cat-${i}`, { passed: false }]),
      ),
      prompt_version: 'v'.repeat(400),
      edges_considered: Array.from({ length: 20 }, (_, i) => ({
        case: `edge${i}`,
        handled: false,
        blind_spot: `issue${i}`,
      })),
    });
    // The script writes block + '\n'; allow the trailing newline.
    expect(out.trimEnd().length).toBeLessThanOrEqual(1500);
    expect(out).toContain('<!-- /blueprint:self-review -->');
  });

  it('emits empty output when JSON is malformed', () => {
    const srPath = path.join(TMPDIR, 'malformed.json');
    fs.writeFileSync(srPath, '{not valid json');
    const out = execFileSync('node', [TMP_SCRIPT, srPath], { encoding: 'utf8' });
    expect(out).toBe('');
  });

});
