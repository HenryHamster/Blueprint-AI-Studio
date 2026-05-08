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

  it('renders reviewer_findings counts by severity (omits zero buckets)', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      reviewer_findings: [
        { category: 'correctness', severity: 'CRITICAL', description: 'bad' },
        { category: 'security', severity: 'HIGH', description: 'worse' },
        { category: 'style-conventions', severity: 'HIGH', description: 'meh' },
      ],
    });
    expect(out).toContain('**Fresh-reviewer findings (high severity):**');
    expect(out).toContain('[CRITICAL] correctness: bad');
    expect(out).toContain('[HIGH] security: worse');
    expect(out).toContain('[HIGH] style-conventions: meh');
    expect(out).not.toContain('MEDIUM:');
    expect(out).not.toContain('LOW:');
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

  it('lists CRITICAL/HIGH severity findings with truncated description', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      reviewer_findings: [
        { category: 'correctness', severity: 'CRITICAL', description: 'Off-by-one error in loop boundary affects all array processing' },
        { category: 'security', severity: 'HIGH', description: 'Unvalidated user input could lead to injection attack' },
      ],
    });
    expect(out).toContain('Fresh-reviewer findings (high severity):');
    expect(out).toContain('[CRITICAL] correctness:');
    expect(out).toContain('[HIGH] security:');
  });

  it('caps high-severity findings list with +N more when exceeding', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      reviewer_findings: [
        { category: 'correctness', severity: 'CRITICAL', description: 'Issue 1' },
        { category: 'security', severity: 'CRITICAL', description: 'Issue 2' },
        { category: 'performance', severity: 'HIGH', description: 'Issue 3' },
        { category: 'type-safety', severity: 'HIGH', description: 'Issue 4' },
      ],
    });
    expect(out).toContain('Fresh-reviewer findings (high severity):');
    expect(out).toContain('+1 more');
    expect(out).toContain('correctness');
    expect(out).toContain('Issue 3');
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

  it('summarizes lower-severity findings as counts in "Other reviewer findings"', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      reviewer_findings: [
        { category: 'documentation', severity: 'MEDIUM', description: 'Missing doc' },
        { category: 'style-conventions', severity: 'LOW', description: 'Formatting' },
        { category: 'style-conventions', severity: 'LOW', description: 'Spacing' },
      ],
    });
    expect(out).toContain('**Other reviewer findings:** MEDIUM: 1, LOW: 2');
  });

  it('prioritizes high-severity findings and unhandled edges over other content', () => {
    const out = runScript({
      verdict: 'fail',
      fixes_applied: false,
      edges_considered: [
        { case: 'unhandled edge', handled: false, blind_spot: 'critical issue' },
      ],
      reviewer_findings: [
        { category: 'correctness', severity: 'CRITICAL', description: 'Critical bug' },
      ],
      check_results: {
        security: { passed: false },
        performance: { passed: false },
      },
    });
    // Unhandled edge should appear before failing categories
    const unhandledIdx = out.indexOf('Unhandled edges');
    const criticalIdx = out.indexOf('Fresh-reviewer findings');
    const failIdx = out.indexOf('Failing categories');
    expect(unhandledIdx).toBeGreaterThan(-1);
    expect(criticalIdx).toBeGreaterThan(-1);
    expect(failIdx).toBeGreaterThan(-1);
    expect(unhandledIdx).toBeLessThan(criticalIdx);
    expect(criticalIdx).toBeLessThan(failIdx);
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

  it('renders fp_rate_hint when present in reviewer_findings', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      reviewer_findings: [
        {
          category: 'correctness',
          severity: 'CRITICAL',
          description: 'Off-by-one error',
          fp_rate_hint: '(FP rate 55% in this project, n=20)',
        },
      ],
    });
    expect(out).toContain('[CRITICAL] correctness: Off-by-one error (FP rate 55% in this project, n=20)');
  });

  it('renders finding without fp_rate_hint when hint is absent (backward compat)', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      reviewer_findings: [
        {
          category: 'security',
          severity: 'HIGH',
          description: 'Missing validation',
        },
      ],
    });
    expect(out).toContain('[HIGH] security: Missing validation');
    expect(out).not.toContain('FP rate');
  });

  it('includes fp_rate_hint only for findings with sufficient sample size', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      reviewer_findings: [
        {
          category: 'style-conventions',
          severity: 'HIGH',
          description: 'Inconsistent formatting',
          fp_rate_hint: '(FP rate 50% in this project, n=5)',
        },
        {
          category: 'type-safety',
          severity: 'CRITICAL',
          description: 'Unsafe cast',
          fp_rate_hint: '(FP rate 65% in this project, n=15)',
        },
      ],
    });
    // Both hints should be present since we're not filtering them here;
    // filtering happens when augmenting in the agent handler
    expect(out).toContain('(FP rate 50% in this project, n=5)');
    expect(out).toContain('(FP rate 65% in this project, n=15)');
  });

  it('truncates description but preserves fp_rate_hint', () => {
    const out = runScript({
      verdict: 'pass',
      fixes_applied: false,
      reviewer_findings: [
        {
          category: 'correctness',
          severity: 'CRITICAL',
          description: 'This is a very long description that will be truncated by the 80-character limit in the renderer',
          fp_rate_hint: '(FP rate 72% in this project, n=25)',
        },
      ],
    });
    expect(out).toContain('(FP rate 72% in this project, n=25)');
    // Description should be truncated to 80 chars
    expect(out).toMatch(/This is a very long description that will be truncated by the 80-c/);
  });
});
