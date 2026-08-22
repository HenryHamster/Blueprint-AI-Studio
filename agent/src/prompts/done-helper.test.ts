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
    expect(doneScript).toContain('"self_review_outcome": json.loads(self_review_raw)');

    // REGRESSING_CATS is always defined (empty when not configured)
    expect(doneScript).toContain('REGRESSING_CATS=()');
    // The combined gate infrastructure is present (no-op when empty)
    expect(doneScript).toContain('MISSING_REASONING=()');
  });

  it('generates ./done with a zero-effective-diff guard on the PR completion path', async () => {
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

    // Reads the recorded base commit and blocks COMPLETED-with-PR when the
    // branch nets to no effective diff, routing to the sanctioned no-op exits.
    expect(doneScript).toContain('_BASE_COMMIT_FILE="/tmp/base_commit_test-task-123"');
    expect(doneScript).toContain('git -C "/tmp/workspace" diff --name-only "$_BASE_COMMIT..HEAD"');
    expect(doneScript).toContain('ZERO effective diff vs base');
    expect(doneScript).toContain('./done --already-done');
    expect(doneScript).toContain('./pr close --superseded-by');
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
    expect(doneScript).toContain('get("check_results",{}).get');
    expect(doneScript).toContain('get("reasoning","")');
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
    // REGRESSING_CATS is always defined (empty when no categories configured)
    expect(doneScript).toContain('REGRESSING_CATS=()');
    // The combined gate runs but regressing check is a no-op with empty array
    expect(doneScript).toContain('MISSING_REASONING');
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
    // REGRESSING_CATS is always defined (empty when no categories configured)
    expect(doneScript).toContain('REGRESSING_CATS=()');
    // The combined gate runs but regressing check is a no-op with empty array
    expect(doneScript).toContain('MISSING_REASONING');
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
    // (batched python3 call with compiled regex instead of per-category bash grep loop)
    expect(doneScript).toContain('anchor_re = re.compile');
    expect(doneScript).toContain('anchor_re.search');
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

// ── writeDoneHelper — consumed_artifact_ids reporting ─────────────────────────
describe('writeDoneHelper — consumed_artifact_ids handling', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('emits consumed_artifact_ids: [] when consumed-artifacts file is malformed (not a JSON array)', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'consumed-test-1',
      workBranch: 'blueprint/consumed-test-1',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    // The malformed-file branch must set CONSUMED_ARTIFACTS_JSON to "[]"
    // rather than leaving it empty (which would omit the field from the payload).
    expect(doneScript).toContain('CONSUMED_ARTIFACTS_JSON="[]"');
    // The error message should mention the empty report outcome
    expect(doneScript).toContain('reporting empty consumed_artifact_ids');
    // The valid-array branch must still filter and deduplicate
    expect(doneScript).toContain('isinstance(x,str) and len(x)>0');
    // The payload construction must include the field when CONSUMED_ARTIFACTS_JSON is set
    expect(doneScript).toContain('payload["consumed_artifact_ids"]');
  });

  it('omits consumed_artifact_ids when consumed-artifacts file does not exist', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'consumed-test-2',
      workBranch: 'blueprint/consumed-test-2',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    // When file doesn't exist, CONSUMED_ARTIFACTS_JSON stays "" so the payload
    // uses null (via ${CONSUMED_ARTIFACTS_JSON:-null}) and the field is omitted.
    expect(doneScript).toContain('CONSUMED_ARTIFACTS_JSON=""');
    expect(doneScript).toContain('${CONSUMED_ARTIFACTS_JSON:-null}');
    // The conditional merge omits the field when consumed_artifacts is null/falsy
    expect(doneScript).toContain('if consumed_raw != "null"');
    expect(doneScript).toContain('payload["consumed_artifact_ids"] = json.loads(consumed_raw)');
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
    expect(doneScript).toContain('"status":"COMPLETED","no_op_reason"');
    // It also emits the canonical reproduce-on-HEAD verdict (already_resolved),
    // reusing the affirmative reason as the verdict reason, additively alongside
    // no_op_reason so older backends are unaffected.
    expect(doneScript).toContain('"status":"already_resolved","reason"');
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
    expect(doneScript).toContain('"reproduce_verdict":');
    expect(doneScript).toContain('"status": repro_status');
    expect(doneScript).toContain('"reason": repro_reason');
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

// ── writeDoneHelper — jq-free JSON payload build ───────────────────────────────
// The strategist VM class does not reliably ship `jq`. The done-helper is the
// critical completion path that signals task success/failure to the orchestrator;
// a broken payload (from a missing jq or bad python3 migration) would silently
// prevent task completion from being recorded. This test proves the generated
// script builds a valid COMPLETED payload with jq REMOVED from PATH.
const SUPPORTS_EXEC_BIT = process.platform !== 'win32';

describe('writeDoneHelper — jq-free JSON payload build', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  /** Generate the done helper, run `./done` with jq poisoned + a curl stub that
   *  captures the POST body, and return the parsed payload. */
  const runDoneNoJq = async (
    cfg: { taskId: string; workBranch: string; baseBranch: string; owner: string; repo: string; testsOnMainBranch: boolean },
  ): Promise<{ exit: number; jqCalled: boolean; payload: Record<string, unknown> | null }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');

    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), `done-nojq-`));
    try {
      // Write the generated done script to the temp dir
      const scriptPath = realPath.join(dir, 'done');
      realFs.writeFileSync(scriptPath, capturedScripts['/tmp/workspace/done'], { mode: 0o755 });

      // Create a fake PR URL file — the done script requires ./pr open to have
      // run first and written /tmp/pr_url_<taskId>.
      const prUrlFile = `/tmp/pr_url_${cfg.taskId}`;
      realFs.writeFileSync(prUrlFile, 'https://github.com/test-owner/test-repo/pull/99');

      // Poison bin: a "poison" jq at the FRONT of PATH records a marker if invoked
      // (so a genuinely jq-free path is proven by the marker's ABSENCE), and a
      // curl stub captures the POST body into a file while returning 200.
      const bin = realPath.join(dir, 'bin');
      realFs.mkdirSync(bin);
      const payloadFile = realPath.join(dir, 'payload.json');
      const jqMarker = realPath.join(dir, 'jq-called');
      realFs.writeFileSync(
        realPath.join(bin, 'curl'),
        `#!/bin/bash\ncat > "${payloadFile}"\nprintf '200'\n`,
        { mode: 0o755 },
      );
      realFs.writeFileSync(
        realPath.join(bin, 'jq'),
        `#!/bin/bash\necho called > "${jqMarker}"\nexit 1\n`,
        { mode: 0o755 },
      );

      const env = {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        ORCHESTRATOR_URL: 'http://orchestrator.test',
        AGENT_API_TOKEN: 'test-token',
      };

      let exit = 0;
      try {
        realCp.execFileSync('bash', [scriptPath], { stdio: 'pipe', env });
      } catch (err: unknown) {
        exit = (err as { status?: number }).status ?? -1;
      }

      const jqCalled = realFs.existsSync(jqMarker);
      const payload: Record<string, unknown> | null = realFs.existsSync(payloadFile)
        ? (JSON.parse(realFs.readFileSync(payloadFile, 'utf8')) as Record<string, unknown>)
        : null;

      // Cleanup /tmp files created by the script on success
      try { realFs.unlinkSync(`/tmp/done_${cfg.taskId}`); } catch { /* ignore */ }
      try { realFs.unlinkSync(`/tmp/task_${cfg.taskId}_result.json`); } catch { /* ignore */ }
      try { realFs.unlinkSync(prUrlFile); } catch { /* ignore */ }

      return { exit, jqCalled, payload };
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
    }
  };

  it.skipIf(!SUPPORTS_EXEC_BIT)('builds valid COMPLETED payload with jq REMOVED from PATH', async () => {
    const { exit, jqCalled, payload } = await runDoneNoJq({
      taskId: `done-nojq-${process.pid}`,
      workBranch: 'blueprint/done-nojq',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });

    expect(exit).toBe(0);
    expect(jqCalled).toBe(false);
    expect(payload).not.toBeNull();
    expect(payload?.status).toBe('COMPLETED');
    expect(payload?.task_id).toBe(`done-nojq-${process.pid}`);
    expect(payload?.pr_url).toBe('https://github.com/test-owner/test-repo/pull/99');
    expect(payload?.artifact_type).toBe('PULL_REQUEST');
    // The self-review fallback envelope must be present so downstream consumers
    // see the completion signal even when the agent skipped self-review.
    expect(payload?.self_review_outcome).toMatchObject({ verdict: 'unknown' });
    // The python3-constructed payload must round-trip fields faithfully —
    // no jq, no silent truncation.
    expect(payload?.logs).toContain('Task complete. PR:');
  });
});

// ── writeDoneHelper — zero-effective-diff guard (execution) ────────────────────
// A rebased/superseded branch can carry a PR URL yet net to no effective diff
// vs base. ./done must NOT post COMPLETED-with-PR in that case; it must block
// and point at the sanctioned no-op exits.
describe('writeDoneHelper — zero-effective-diff guard', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it.skipIf(!SUPPORTS_EXEC_BIT)('blocks COMPLETED and does not POST when base..HEAD is empty', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');

    const taskId = `done-emptydiff-${process.pid}`;
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'done-emptydiff-'));
    const prUrlFile = `/tmp/pr_url_${taskId}`;
    const baseCommitFile = `/tmp/base_commit_${taskId}`;
    try {
      const scriptPath = realPath.join(dir, 'done');
      realFs.writeFileSync(scriptPath, capturedScripts['/tmp/workspace/done'], { mode: 0o755 });
      realFs.writeFileSync(prUrlFile, 'https://github.com/test-owner/test-repo/pull/99');
      realFs.writeFileSync(baseCommitFile, 'abc123');

      const bin = realPath.join(dir, 'bin');
      realFs.mkdirSync(bin);
      const curlMarker = realPath.join(dir, 'curl-called');
      // git stub: the base-relative diff is EMPTY (rebased/superseded branch).
      realFs.writeFileSync(
        realPath.join(bin, 'git'),
        `#!/bin/bash\ncase "$*" in\n  *"diff --name-only abc123..HEAD"*) exit 0 ;;\n  *) exit 0 ;;\nesac\n`,
        { mode: 0o755 },
      );
      // curl stub: records if ever invoked — the COMPLETED POST must NOT happen.
      realFs.writeFileSync(
        realPath.join(bin, 'curl'),
        `#!/bin/bash\necho called > "${curlMarker}"\nprintf '200'\n`,
        { mode: 0o755 },
      );

      const env = {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        ORCHESTRATOR_URL: 'http://orchestrator.test',
        AGENT_API_TOKEN: 'test-token',
      };

      let exit = 0;
      let stderr = '';
      try {
        realCp.execFileSync('bash', [scriptPath], { stdio: 'pipe', env });
      } catch (err: unknown) {
        const e = err as { status?: number; stderr?: Buffer };
        exit = e.status ?? -1;
        stderr = e.stderr?.toString() ?? '';
      }

      expect(exit).toBe(2);
      expect(stderr).toContain('ZERO effective diff vs base');
      expect(stderr).toContain('./done --already-done');
      // No COMPLETED POST fired, and no done marker was written.
      expect(realFs.existsSync(curlMarker)).toBe(false);
      expect(realFs.existsSync(`/tmp/done_${taskId}`)).toBe(false);
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
      try { realFs.unlinkSync(prUrlFile); } catch { /* ignore */ }
      try { realFs.unlinkSync(baseCommitFile); } catch { /* ignore */ }
      try { realFs.unlinkSync(`/tmp/done_${taskId}`); } catch { /* ignore */ }
    }
  });
});

// ── Regression: ./done --warn-out-of-scope cannot bypass a failing ./check ────
//
// Root cause (PR #16801 / task b9a990ed): ./done itself does not run ./check.
// It relies on ./pr open having gated on ./check before writing the PR URL file.
// Two bypass paths existed in run_pr_gate (the gate inside ./pr open):
//   1. ./check times out (exit 124/137) → was treated as pass-through (return 0)
//   2. timeout not installed → GATE_STATUS was set to 127 without running ./check,
//      then 127 was treated as "unavailable" (return 0)
// In both cases ./pr open wrote the PR URL file, which ./done reads to proceed.
// --warn-out-of-scope was then used to complete the submission with a failing check.
//
// The fix (in pr-helper.ts run_pr_gate): timeout now blocks; timeout-absent now
// runs ./check directly without a deadline. These tests assert that ./done (and
// specifically --warn-out-of-scope) CANNOT complete when no PR URL file exists,
// which is the state produced by a failing ./pr open gate.
describe('writeDoneHelper — --warn-out-of-scope does not bypass the check gate', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  // Verify the generated script contains the PR URL requirement BEFORE the
  // --warn-out-of-scope message is posted. This confirms that --warn-out-of-scope
  // does not create a separate bypass path around the PR URL gate.
  it('generated ./done places --warn-out-of-scope FEEDBACK post AFTER the PR URL requirement', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'woos-static-1',
      workBranch: 'blueprint/woos-static-1',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    // The PR URL file check must precede the --warn-out-of-scope FEEDBACK post.
    const prFileIdx = doneScript.indexOf('PR_FILE="/tmp/pr_url_woos-static-1"');
    const warnPostIdx = doneScript.indexOf('warn-out-of-scope FEEDBACK message post');
    expect(prFileIdx).toBeGreaterThan(-1);
    expect(warnPostIdx).toBeGreaterThan(-1);
    expect(prFileIdx).toBeLessThan(warnPostIdx);

    // The PR URL check exits non-zero when the file is missing — --warn-out-of-scope
    // cannot proceed past that barrier.
    const prFileBlock = doneScript.slice(prFileIdx, prFileIdx + 200);
    expect(prFileBlock).toContain('run ./pr open first');
    expect(prFileBlock).toContain('exit 2');
  });

  it('generated ./done distinguishes receipt-backed inherited failures from blocking failures', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'done-guidance-attribution',
      workBranch: 'blueprint/done-guidance-attribution',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toContain('records named inherited failures');
    expect(doneScript).toContain('verification receipt');
    expect(doneScript).toContain('Do not use this flag to');
    expect(doneScript).toContain('new, unattributed, execution, or explicit coverage failures');
    expect(doneScript).not.toContain('ship despite ./check failures');
  });

  it('--warn-out-of-scope flag is parsed but does not skip the PR URL file requirement', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'woos-static-2',
      workBranch: 'blueprint/woos-static-2',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();

    // --warn-out-of-scope is parsed into WARN_OUT_OF_SCOPE_REASON.
    expect(doneScript).toContain('WARN_OUT_OF_SCOPE_REASON');
    expect(doneScript).toContain('--warn-out-of-scope');

    // The flag is processed on the --already-done path (which runs before PR URL check)
    // and on the main path (which runs after PR URL check). Verify that NO branch exists
    // that sets WARN_OUT_OF_SCOPE_REASON before the PR file gate and then skips it.
    const alreadyDoneExitIdx = doneScript.indexOf('exit 0\n', doneScript.indexOf('ALREADY_DONE_REASON'));
    const prFileIdx = doneScript.indexOf('PR_FILE="/tmp/pr_url_woos-static-2"');
    // The --already-done branch has its own exit; the PR file check must still be
    // present and must come after that branch exits cleanly.
    expect(alreadyDoneExitIdx).toBeGreaterThan(-1);
    expect(prFileIdx).toBeGreaterThan(-1);
    // The PR file check must occur after the already-done branch.
    expect(prFileIdx).toBeGreaterThan(alreadyDoneExitIdx);
  });

  // Execution test: ./done --warn-out-of-scope must block (exit non-zero) when
  // the PR URL file does not exist. This is the state left by a failing ./pr open
  // (which happens when ./check fails, times out, or yields no verdict).
  it.skipIf(process.platform === 'win32')(
    'blocks (exit 2) when PR URL file is absent — same behavior with or without --warn-out-of-scope',
    async () => {
      const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
      const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
      const realOs = await vi.importActual<typeof import('node:os')>('node:os');
      const realPath = await vi.importActual<typeof import('node:path')>('node:path');
      const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');

      const taskId = `woos-exec-${process.pid}`;
      writeCodeChangeHelpers({
        taskId,
        workBranch: `blueprint/${taskId}`,
        baseBranch: 'main',
        owner: 'test-owner',
        repo: 'test-repo',
        testsOnMainBranch: false,
      });
      const doneScript = capturedScripts['/tmp/workspace/done'];
      const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'done-woos-'));
      const prUrlFile = `/tmp/pr_url_${taskId}`;
      try {
        const scriptPath = realPath.join(dir, 'done');
        realFs.writeFileSync(scriptPath, doneScript, { mode: 0o755 });
        // Ensure no PR URL file exists (simulates ./pr open having been blocked by check).
        try { realFs.unlinkSync(prUrlFile); } catch { /* ok */ }

        const runDone = (extraArgs: string[]): { exit: number; stderr: string } => {
          let exit = 0;
          let stderr = '';
          try {
            realCp.execFileSync('bash', [scriptPath, ...extraArgs], {
              stdio: 'pipe',
              env: {
                ...process.env,
                ORCHESTRATOR_URL: 'http://orchestrator.test',
                AGENT_API_TOKEN: 'test-token',
              },
            });
          } catch (err: unknown) {
            const e = err as { status?: number; stderr?: Buffer };
            exit = e.status ?? -1;
            stderr = e.stderr?.toString() ?? '';
          }
          return { exit, stderr };
        };

        // Without --warn-out-of-scope: must block.
        const plain = runDone([]);
        expect(plain.exit).toBe(2);
        expect(plain.stderr).toContain('run ./pr open first');

        // With --warn-out-of-scope: must ALSO block — the flag does not bypass the
        // PR URL requirement that ./pr open writes after a passing ./check gate.
        const reason = 'pre-existing ruff failures in unrelated module — out of scope';
        const woos = runDone(['--warn-out-of-scope', reason]);
        expect(woos.exit).toBe(2);
        expect(woos.stderr).toContain('run ./pr open first');

        // The COMPLETED post must never have been attempted.
        expect(realFs.existsSync(`/tmp/done_${taskId}`)).toBe(false);
      } finally {
        realFs.rmSync(dir, { recursive: true, force: true });
        try { realFs.unlinkSync(prUrlFile); } catch { /* ok */ }
        try { realFs.unlinkSync(`/tmp/done_${taskId}`); } catch { /* ok */ }
      }
    },
  );

  it('--warn-out-of-scope reason must be at least 30 chars (existing gate still applies)', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'woos-static-3',
      workBranch: 'blueprint/woos-static-3',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });

    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    // The minimum-length guard is in place — a short reason can't launder a check failure.
    expect(doneScript).toContain('--warn-out-of-scope reason must be at least 30 chars');
    expect(doneScript).toContain('lt 30');
    expect(doneScript).toContain('exit 2');
  });
});
