import { vi, describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    getAgentToken: vi.fn().mockReturnValue(''),
    git: vi.fn().mockReturnValue(''),
  };
});

vi.mock('fs', () => ({
  default: {
    existsSync: vi.fn().mockReturnValue(false),
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    appendFileSync: vi.fn(),
    mkdirSync: vi.fn(),
    readdirSync: vi.fn().mockReturnValue([]),
    unlinkSync: vi.fn(),
  },
}));

describe('writeDoneHelper — self-review fallback', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('generates ./done script with soft-fallback self-review handling', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    // Verify that the script checks for self-review file (with interpolated task ID)
    expect(doneScript).toContain('SELF_REVIEW_FILE="/tmp/self_review_test-task-123.json"');

    // Verify that it uses fallback when file is missing
    expect(doneScript).toContain('self-review skipped — investigate');

    // Verify that it uses fallback when file is invalid JSON
    expect(doneScript).toContain('self-review JSON parsing failed');

    // Verify structured skip_reason in parse-failure fallback
    expect(doneScript).toContain('"skip_reason":"json_parse_failed"');

    // Verify marker-file logic for missing SR file (json_missing default + override)
    expect(doneScript).toContain('SKIP_REASON="json_missing"');
    expect(doneScript).toContain('self_review_skip_reason_test-task-123.txt');

    // Verify that it always includes self_review_outcome in payload
    expect(doneScript).toContain('{self_review_outcome: $self_review}');

    // Without regressingCategories, the gate is absent
    expect(doneScript).not.toContain('REGRESSING_CATS=');
    expect(doneScript).toContain('# No regressing categories for this task type');
  });

  it('generates ./done script with PR URL shape validation', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    // The regex variable must be present and reject non-pull URLs.
    expect(doneScript).toContain("PR_URL_RE='^https://github");
    expect(doneScript).toContain('/pull/');
    // Validation must exit non-zero on mismatch.
    expect(doneScript).toContain('grep -qE "$PR_URL_RE"');
    expect(doneScript).toContain('canonical pull URL');
    // Reference to co-located validation in code-change.ts.
    expect(doneScript).toContain('code-change.ts');
  });

  it('generates ./done with regressing-category reasoning gate when regressingCategories is set', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      regressingCategories: ['correctness', 'test-coverage'],
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    // Gate is embedded with the specific categories
    expect(doneScript).toContain('REGRESSING_CATS=("correctness" "test-coverage")');
    // Validation checks reasoning length and exits 1 on missing
    expect(doneScript).toContain('.check_results[$c].reasoning // ""');
    expect(doneScript).toContain('Regressing categories need a reasoning field');
    expect(doneScript).toContain('exit 1');
    // Task-ID-specific error message for recovery instructions
    expect(doneScript).toContain('self_review_test-task-123.json');
  });
});

// ── writeDoneHelper — regressingCategories edge cases ─────────────────────────
describe('writeDoneHelper — regressingCategories edge cases', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('generates ./done without reasoning gate when regressingCategories is empty array', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'edge-task-1',
      workBranch: 'blueprint/edge-task-1',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      regressingCategories: [],
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).not.toContain('REGRESSING_CATS=');
    expect(doneScript).toContain('# No regressing categories for this task type');
  });

  it('generates ./done with single regressing category in the gate', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'edge-task-2',
      workBranch: 'blueprint/edge-task-2',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      regressingCategories: ['correctness'],
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('REGRESSING_CATS=("correctness")');
    expect(doneScript).toContain('Regressing categories need a reasoning field');
    expect(doneScript).toContain('exit 1');
  });

  it('generates ./done with evidence-anchor gate for high-stakes categories', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'edge-task-3',
      workBranch: 'blueprint/edge-task-3',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      regressingCategories: [],
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('HIGH_STAKES_CATS=');
    expect(doneScript).toContain('correctness');
    expect(doneScript).toContain('error-handling');
    expect(doneScript).toContain('security');
    expect(doneScript).toContain('regression_risk');
    expect(doneScript).toContain('scope-drift');
    expect(doneScript).toContain('evidence anchor');
    expect(doneScript).toContain('MISSING_EVIDENCE');
  });

  it('generates ./done with scope-drift overage gate for large threshold', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'edge-task-4',
      workBranch: 'blueprint/edge-task-4',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('scope-drift overage gate');
    expect(doneScript).toContain('800 lines');
    expect(doneScript).toContain('15 files');
    expect(doneScript).toContain('_SCOPE_DRIFT_PASSED');
  });
});

// ── writeDoneHelper — thinning categories gate ─────────────────────────
describe('writeDoneHelper — thinning categories gate', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('generates ./done with reasoning gate when only thinning categories are set', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'thin-task-1',
      workBranch: 'blueprint/thin-task-1',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      // Only thinning categories (no officially-regressing ones) passed as the union
      regressingCategories: ['test-coverage'],
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('REGRESSING_CATS=("test-coverage")');
    expect(doneScript).toContain('Regressing categories need a reasoning field');
    expect(doneScript).toContain('exit 1');
  });

  it('generates ./done without reasoning gate when both regressing and thinning lists are empty', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'thin-task-2',
      workBranch: 'blueprint/thin-task-2',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      regressingCategories: [],
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).not.toContain('REGRESSING_CATS=');
    expect(doneScript).toContain('# No regressing categories for this task type');
  });

  it('generates ./done with anchor-exception in reasoning gate so short file:line reasoning passes', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'thin-task-3',
      workBranch: 'blueprint/thin-task-3',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      regressingCategories: ['correctness'],
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    // The anchor-detection block must be present inside the reasoning gate
    expect(doneScript).toContain('_HAS_ANCHOR=0');
    expect(doneScript).toContain('[a-zA-Z0-9._/-]+[.][a-z]+:[0-9]+');
    // Short reasoning without anchor still fails
    expect(doneScript).toContain('MISSING_REASONING');
    expect(doneScript).toContain('exit 1');
  });

  it('reasoning gate failure message mentions file:line anchor as valid alternative', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'thin-task-4',
      workBranch: 'blueprint/thin-task-4',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      regressingCategories: ['scope-drift'],
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('file:line anchor');
  });
});

// ── writeDoneHelper — --already-done (affirmative no-op completion) ────────────
describe('writeDoneHelper — --already-done no-op completion', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('emits an --already-done branch that posts COMPLETED with no PR and a no_op_reason', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'noop-task-1',
      workBranch: 'blueprint/noop-task-1',
      baseBranch: 'plan/3a47191b',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    // The flag is parsed.
    expect(doneScript).toContain('--already-done');
    expect(doneScript).toContain('ALREADY_DONE_REASON');
    // The no-op branch posts COMPLETED with a no_op_reason field and NO pr_url.
    expect(doneScript).toContain('status: "COMPLETED", no_op_reason: $r');
    // It also emits the canonical reproduce-on-HEAD verdict (already_resolved),
    // reusing the affirmative reason as the verdict reason, additively alongside
    // no_op_reason so older backends are unaffected.
    expect(doneScript).toContain('reproduce_verdict: {status: "already_resolved", reason: $r}');
    // The branch runs BEFORE the PR-file requirement (so it does not need ./pr open).
    const noopIdx = doneScript.indexOf('if [ -n "$ALREADY_DONE_REASON" ]; then');
    const prFileIdx = doneScript.indexOf('PR_FILE="/tmp/pr_url_noop-task-1"');
    expect(noopIdx).toBeGreaterThan(-1);
    expect(prFileIdx).toBeGreaterThan(-1);
    expect(noopIdx).toBeLessThan(prFileIdx);
    // It writes the done marker on success so the wrapper exits cleanly.
    expect(doneScript).toContain('touch /tmp/done_noop-task-1');
    // Operator-facing status text explains the missing PR.
    expect(doneScript).toContain('deliverable already present');
  });

  it('supports explicit --reproduced and --indeterminate verdict flags on the normal PR completion path', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'noop-task-verdicts',
      workBranch: 'blueprint/noop-task-verdicts',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('--reproduced');
    expect(doneScript).toContain('--indeterminate');
    expect(doneScript).toContain('REPRODUCED_REASON');
    expect(doneScript).toContain('INDETERMINATE_REASON');
    expect(doneScript).toContain('REPRODUCE_VERDICT_STATUS="reproduced"');
    expect(doneScript).toContain('REPRODUCE_VERDICT_STATUS="indeterminate"');
    expect(doneScript).toContain('{reproduce_verdict: {status: $reproduce_status, reason: $reproduce_reason}}');
    expect(doneScript).toContain('mutually exclusive');
  });

  it('requires an affirmative reason of at least 30 chars for --already-done', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'noop-task-2',
      workBranch: 'blueprint/noop-task-2',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    // Empty-reason guard.
    expect(doneScript).toContain('--already-done requires a reason argument');
    // Minimum-length guard (≥30) so a vacuous "done" cannot land COMPLETED.
    expect(doneScript).toContain('--already-done reason must be at least 30 chars');
    expect(doneScript).toContain('lt 30');
  });

  it('requires reasons for the explicit reproduced and indeterminate verdict flags', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'noop-task-2b',
      workBranch: 'blueprint/noop-task-2b',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('--reproduced requires a reason argument');
    expect(doneScript).toContain('--indeterminate requires a reason argument');
  });

  it('makes --already-done and --warn-out-of-scope mutually exclusive', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'noop-task-3',
      workBranch: 'blueprint/noop-task-3',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('mutually exclusive');
  });

  it('does not require ./pr open for the --already-done path (no PR_FILE gate inside the branch)', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'noop-task-4',
      workBranch: 'blueprint/noop-task-4',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    // The no-op branch must exit 0 before the standard PR-required path runs.
    const branch = doneScript.slice(
      doneScript.indexOf('if [ -n "$ALREADY_DONE_REASON" ]; then'),
      doneScript.indexOf('PR_FILE="/tmp/pr_url_noop-task-4"'),
    );
    expect(branch).toContain('exit 0');
    // And the no-op branch never reads the pr_url marker file.
    expect(branch).not.toContain('pr_url_noop-task-4');
  });

  it('generated ./done script (with the --already-done branch) passes `bash -n` syntax check', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'noop-task-5',
      workBranch: 'blueprint/noop-task-5',
      baseBranch: 'plan/3a47191b',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
      regressingCategories: ['correctness'],
    });
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/done_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, doneScript);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });
});
