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
