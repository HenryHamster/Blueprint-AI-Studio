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

// ── Imports (after mocks) ─────────────────────────────────────────────────────
import {
  detectLinterKind,
  buildScopedLintOneliner,
} from './code-change-helpers.js';

describe('writeCodeChangeHelpers — self-review fallback', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('generates ./check with the admin-ui viewport gate and no jsdom test command', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });

    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    expect(checkScript).toContain('run "admin-ui playwright chromium"');
    expect(checkScript).toContain('npm run test:viewport');
    expect(checkScript).toContain('Playwright Chromium');
    // Bootstrap step uses the smart install logic (cache check + fallback chain).
    expect(checkScript).toContain('$HOME/.cache/ms-playwright');
    expect(checkScript).toContain('npx playwright install chromium');
    expect(checkScript).toContain('npx playwright install --with-deps chromium');
    expect(checkScript).toContain('Retrying inside');
    expect(checkScript).toContain('mcr.microsoft.com/playwright:v1.59.1-jammy');
    expect(checkScript).not.toContain('npm run test:run');
    expect(checkScript.indexOf('admin-ui playwright chromium')).toBeLessThan(checkScript.indexOf('admin-ui viewport'));
    expect(checkScript.indexOf('admin-ui viewport')).toBeLessThan(checkScript.indexOf('admin-ui build'));
  });

  it('generated ./check script passes `bash -n` syntax check', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });

    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    // Use the real temp dir (forward-slashed) instead of a hardcoded /tmp so
    // Node's writeFileSync and bash resolve to the same path on Windows.
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/check_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, checkScript);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });

  it('includes missing viewport test check in ./check script', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });

    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();

    // Check script should contain the new component viewport test guard
    expect(checkScript).toContain('Check for new admin-ui components without viewport tests');
    expect(checkScript).toContain('admin-ui/src/components/[^/]+\\.tsx');
    expect(checkScript).toContain('@viewport-test-exempt');
    expect(checkScript).toContain('Expected test file:');
    expect(checkScript).toContain('admin-ui/src/test/viewport.ts');
    // Loop must run in the parent shell (process substitution), and a
    // missing sibling test must populate FAILED so the script-level
    // gate exits non-zero. A piped `while` would lose the FAILED+= in a
    // subshell and the gate would only print a warning.
    expect(checkScript).toContain('done < <(printf');
    expect(checkScript).toContain('FAILED+=("admin-ui new component viewport test")');
    expect(checkScript).not.toMatch(/\| while read -r comp; do[\s\S]*?exit 1/);
  });

  it('./check new-component gate exits non-zero when sibling viewport test is missing', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();

    // Extract the new-component gate block in isolation. We can't easily
    // run the entire check script (it sets up git diffs, npm ci, etc.);
    // running just the gate with stubbed inputs is enough to verify the
    // pipeline-vs-process-substitution fix.
    const startMarker = '# Check for new admin-ui components without viewport tests';
    const endMarker = ': > /tmp/check_viewport_tests.txt';
    const startIdx = checkScript.indexOf(startMarker);
    const endIdx = checkScript.indexOf(endMarker, startIdx);
    expect(startIdx).toBeGreaterThan(-1);
    expect(endIdx).toBeGreaterThan(startIdx);
    const gate = checkScript.slice(startIdx, endIdx);

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');

    const runGate = (setupFiles: Array<[string, string]>): number => {
      const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'check-newcomp-'));
      try {
        for (const [rel, body] of setupFiles) {
          const abs = realPath.join(dir, rel);
          realFs.mkdirSync(realPath.dirname(abs), { recursive: true });
          realFs.writeFileSync(abs, body);
        }
        const harness = [
          'set -uo pipefail',
          `cd "${dir}"`,
          'FULL_SCOPE=0',
          'ADMIN_UI_CHANGED="admin-ui/src/components/Foo.tsx"',
          'FAILED=()',
          gate,
          'exit ${#FAILED[@]}',
        ].join('\n');
        const tmp = realPath.join(dir, 'harness.sh');
        realFs.writeFileSync(tmp, harness);
        try {
          realCp.execFileSync('bash', [tmp], { stdio: 'pipe' });
          return 0;
        } catch (err: unknown) {
          const e = err as { status?: number };
          return e.status ?? -1;
        }
      } finally {
        realFs.rmSync(dir, { recursive: true, force: true });
      }
    };

    // Missing sibling viewport test → gate must register a failure.
    expect(runGate([
      ['admin-ui/src/components/Foo.tsx', 'export const Foo = () => null;\n'],
    ])).toBeGreaterThan(0);

    // Sibling viewport test present → gate must pass cleanly.
    expect(runGate([
      ['admin-ui/src/components/Foo.tsx', 'export const Foo = () => null;\n'],
      ['admin-ui/src/components/Foo.viewport.test.tsx', ''],
    ])).toBe(0);

    // Exemption marker → gate must pass cleanly.
    expect(runGate([
      ['admin-ui/src/components/Foo.tsx', '// @viewport-test-exempt\nexport const Foo = () => null;\n'],
    ])).toBe(0);
  });

  it('generates migration chain guard block in ./check when alembic/versions changed', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    // Guard is inside the backend block and gated on alembic/versions
    expect(checkScript).toContain('backend/alembic/versions/');
    expect(checkScript).toContain('python3 backend/app/migration_chain.py');
    expect(checkScript).toContain('backend migration chain');
  });

  it('migration chain guard is absent from buildConfig-driven check (non-blueprint repos)', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      buildConfig: {
        directories: [{ path: 'src', type: 'node' }],
      },
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    expect(checkScript).not.toContain('python3 backend/app/migration_chain.py');
  });

  it('generates ./pr script with SR section injection for new PR path', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });
    const prScript = capturedScripts['/tmp/workspace/pr'];
    expect(prScript).toBeDefined();

    // SR file is checked before injection
    expect(prScript).toContain('SR_FILE="/tmp/self_review_test-task-123.json"');
    expect(prScript).toContain('SR_SKIP_FILE="/tmp/self_review_skip_reason_test-task-123.txt"');

    // base64-decoded node script renders the section
    expect(prScript).toContain('base64 -d > /tmp/sr_section_test-task-123.js');
    expect(prScript).toContain('node /tmp/sr_section_test-task-123.js');

    // Prior marker block is stripped before appending (closing marker appears escaped in sed)
    expect(prScript).toContain('<!-- blueprint:self-review -->');
    expect(prScript).toContain("\\/blueprint:self-review");
    expect(prScript).toContain("sed '/<!-- blueprint:self-review -->/");
  });

  it('generates ./pr script with existing-PR PATCH logic for review-fix path', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'fix-task-456',
      workBranch: 'blueprint/fix-task-456',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      existingPrNumber: 42,
      testsOnMainBranch: false,
    });
    const prScript = capturedScripts['/tmp/workspace/pr'];
    expect(prScript).toBeDefined();

    // SR injection bash is still present (shared with new-PR path)
    expect(prScript).toContain('SR_FILE="/tmp/self_review_fix-task-456.json"');

    // Existing-PR path: PATCH call updates PR body
    expect(prScript).toContain('PATCH');
    expect(prScript).toContain('/pulls/42');
    expect(prScript).toContain('SR body update');
  });
  it('generates ./check and ./pr with test gate when promotionTarget matches baseBranch', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-promo',
      workBranch: 'blueprint/test-task-promo',
      baseBranch: 'production',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      promotionTarget: 'production',
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    const prScript = capturedScripts['/tmp/workspace/pr'];
    expect(checkScript).toBeDefined();
    expect(prScript).toBeDefined();
    // Test gate fires because baseBranch === promotionTarget === 'production'
    expect(checkScript).toContain('"1" = "1"');
    expect(prScript).toContain('LTS branch');
    expect(prScript).toContain('mandatory test gate');
  });

  it('does not generate test gate when promotionTarget does not match baseBranch', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-feat',
      workBranch: 'blueprint/test-task-feat',
      baseBranch: 'feature/xyz',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      promotionTarget: 'production',
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    const prScript = capturedScripts['/tmp/workspace/pr'];
    // baseBranch !== promotionTarget, so no mandatory gate
    expect(checkScript).toContain('"0" = "1"');
    expect(prScript).not.toContain('mandatory test gate');
  });
});

// ── writeCodeChangeHelpers — shell injection validation ───────────────────────
describe('writeCodeChangeHelpers — shell injection validation', () => {
  const validCfg = {
    taskId: 'task-abc123',
    workBranch: 'blueprint/task-abc123',
    baseBranch: 'main',
    owner: 'acme-org',
    repo: 'my-repo',
    testsOnMainBranch: false,
  };

  beforeEach(() => {
    vi.mocked(fs.writeFileSync).mockImplementation(() => {});
  });

  it('accepts valid cfg fields without throwing', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers(validCfg); }).not.toThrow();
  });

  it('rejects taskId with shell metacharacters', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers({ ...validCfg, taskId: 'task;rm -rf /' }); }).toThrow(/taskId/);
  });

  it('rejects taskId with dollar sign', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers({ ...validCfg, taskId: 'task$VAR' }); }).toThrow(/taskId/);
  });

  it('rejects workBranch with shell metacharacters', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers({ ...validCfg, workBranch: 'branch`whoami`' }); }).toThrow(/workBranch/);
  });

  it('rejects baseBranch with newline', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers({ ...validCfg, baseBranch: 'main\nevil' }); }).toThrow(/baseBranch/);
  });

  it('rejects owner with shell metacharacters', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers({ ...validCfg, owner: 'owner$(id)' }); }).toThrow(/owner/);
  });

  it('rejects repo with shell metacharacters', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers({ ...validCfg, repo: 'repo&&evil' }); }).toThrow(/repo/);
  });

  it('accepts branch names with slashes and dots', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers({ ...validCfg, workBranch: 'feat/my-feature.v2', baseBranch: 'release/1.0' }); }).not.toThrow();
  });

  it('accepts owner/repo with dots and hyphens', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => { writeCodeChangeHelpers({ ...validCfg, owner: 'my.org-123', repo: 'my-repo.v2' }); }).not.toThrow();
  });

  it('rejects build_config command with shell metacharacters', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => {
      writeCodeChangeHelpers({
        ...validCfg,
        buildConfig: { directories: [{ path: 'svc', type: 'node', build_cmd: 'npm run build; rm -rf /' }] },
      });
    }).toThrow(/build_cmd/);
  });

  it('rejects build_config env_var value with command substitution', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => {
      writeCodeChangeHelpers({
        ...validCfg,
        buildConfig: { directories: [{ path: 'svc', type: 'node' }], env_vars: { API: '$(whoami)' } },
      });
    }).toThrow(/env_var value/);
  });

  it('rejects build_config env_var name with shell metacharacters', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => {
      writeCodeChangeHelpers({
        ...validCfg,
        buildConfig: { directories: [{ path: 'svc', type: 'node' }], env_vars: { 'A;B': 'x' } },
      });
    }).toThrow(/env_var name/);
  });

  it('rejects build_config directory path with shell metacharacters', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => {
      writeCodeChangeHelpers({
        ...validCfg,
        buildConfig: { directories: [{ path: 'svc`whoami`', type: 'node', build_cmd: 'npm run build' }] },
      });
    }).toThrow(/directory path/);
  });

  it('accepts build_config with typical install/lint/build/test commands', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => {
      writeCodeChangeHelpers({
        ...validCfg,
        buildConfig: {
          directories: [{
            path: 'service/api',
            type: 'python',
            install_cmd: 'pip install -e .',
            lint_cmd: 'python3 -m ruff check --select E,F,W --ignore E501',
            build_cmd: 'python3 -m build',
            test_cmd: 'pytest -q',
          }],
          env_vars: { API_URL: 'http://localhost:8080', NODE_ENV: 'ci' },
        },
      });
    }).not.toThrow();
  });
});

// ── writeCheckHelper — config-driven (build_config) path ──────────────────────
describe('writeCheckHelper — config-driven build_config path', () => {
  let capturedScripts: Record<string, string> = {};

  const buildConfig = {
    directories: [
      {
        path: 'service/api',
        type: 'python',
        install_cmd: 'pip install -e .',
        lint_cmd: 'python3 -m ruff check',
        build_cmd: 'python3 -m build',
        test_cmd: 'pytest -q',
      },
      {
        path: 'web/',
        type: 'node',
        install_cmd: 'npm ci',
        lint_cmd: 'npm run lint',
        build_cmd: 'npm run build',
      },
    ],
    env_vars: { API_URL: 'http://localhost:8080', NODE_ENV: 'ci' },
  };

  const baseCfg = {
    taskId: 'task-cfg-1',
    workBranch: 'blueprint/task-cfg-1',
    baseBranch: 'main',
    owner: 'acme',
    repo: 'other-repo',
    testsOnMainBranch: true,
  };

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('generates per-directory blocks from buildConfig and omits hardcoded Blueprint blocks', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg, buildConfig });

    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    // Configured directories + commands are present.
    expect(checkScript).toContain('if touches "service/api/"; then');
    expect(checkScript).toContain(`run "service/api install" bash -c 'cd "service/api" && pip install -e .'`);
    // Recognized ruff lint_cmd is scoped to changed files (FULL_SCOPE=1 runs verbatim).
    expect(checkScript).toContain('if [ "$FULL_SCOPE" = "1" ]; then');
    expect(checkScript).toContain(`run "service/api lint" bash -c 'cd "service/api" && python3 -m ruff check'`);
    expect(checkScript).toContain('ruff check --select E,F,W --ignore E501');
    expect(checkScript).toContain(`run "service/api build" bash -c 'cd "service/api" && python3 -m build'`);
    // Trailing slash in the configured path is normalized.
    expect(checkScript).toContain('if touches "web/"; then');
    expect(checkScript).toContain(`run "web build" bash -c 'cd "web" && npm run build'`);
    // The hardcoded Blueprint stack blocks must NOT run on the config-driven
    // path (the `admin_ui_runtime_touched` helper is still defined in the
    // shared header, but it must not be invoked, and no hardcoded stack
    // commands should appear).
    expect(checkScript).not.toContain('if admin_ui_runtime_touched; then');
    expect(checkScript).not.toContain('run "agent tsc"');
    expect(checkScript).not.toContain('backend ruff (changed)');
    expect(checkScript).not.toContain('npm run test:viewport');
  });

  it('falls back to hardcoded Blueprint blocks when buildConfig is undefined', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg });

    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    expect(checkScript).toContain('admin_ui_runtime_touched');
    expect(checkScript).toContain('run "agent tsc"');
    expect(checkScript).toContain('backend ruff (changed)');
    expect(checkScript).toContain('npm run test:viewport');
    // And the config-driven marker is absent.
    expect(checkScript).not.toContain('Config-driven checks');
  });

  it('falls back to hardcoded blocks when buildConfig has empty directories', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg, buildConfig: { directories: [] } });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toContain('admin_ui_runtime_touched');
    expect(checkScript).not.toContain('Config-driven checks');
  });

  it('gates configured test_cmd on ./check test (WITH_TESTS)', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg, buildConfig });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    // test_cmd is wrapped in a WITH_TESTS guard.
    expect(checkScript).toContain('if [ "$WITH_TESTS" = "1" ]; then');
    expect(checkScript).toContain(`run "service/api test" bash -c 'cd "service/api" && pytest -q'`);
    // The directory with no test_cmd produces no test run line.
    expect(checkScript).not.toContain('web test');
  });

  it('exports env_vars from buildConfig in the generated ./check', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg, buildConfig });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toContain(`export API_URL='http://localhost:8080'`);
    expect(checkScript).toContain(`export NODE_ENV='ci'`);
  });

  it('generated config-driven ./check passes `bash -n` syntax check', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg, buildConfig });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/check_cfg_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, checkScript);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });
});

// ── detectLinterKind ──────────────────────────────────────────────────────────
describe('detectLinterKind', () => {
  it.each([
    ['ruff check .', 'ruff'],
    ['python3 -m ruff check .', 'ruff'],
    ['python -m ruff check', 'ruff'],
    ['npx eslint .', 'eslint'],
    ['pnpm exec eslint .', 'eslint'],
    ['mypy .', 'mypy'],
    ['python3 -m mypy src/', 'mypy'],
    ['flake8 .', 'flake8'],
    ['pyright .', 'pyright'],
    ['npx pyright', 'pyright'],
  ])('detects %s as %s', (cmd, expected) => {
    expect(detectLinterKind(cmd)).toBe(expected);
  });

  it.each([
    ['npm run lint'],
    ['custom-lint-script'],
    ['bash lint.sh'],
  ])('returns null for unrecognized command %s', (cmd) => {
    expect(detectLinterKind(cmd)).toBeNull();
  });
});

// ── buildScopedLintOneliner ────────────────────────────────────────────────────
describe('buildScopedLintOneliner', () => {
  it('returns null for unrecognized lint_cmd', () => {
    expect(buildScopedLintOneliner('bash custom-lint.sh', 'svc', 'main', 'git-diff')).toBeNull();
    expect(buildScopedLintOneliner('npm run lint', 'svc', 'main', 'check-tmpfile')).toBeNull();
  });

  it.each([
    ['ruff check .', 'svc', 'main'],
    ['python3 -m mypy', 'backend', 'develop'],
    ['flake8 .', 'src', 'main'],
    ['npx eslint .', 'admin-ui', 'main'],
    ['pyright .', 'agent', 'main'],
  ] as [string, string, string][])(
    'check-tmpfile output for %s in dir=%s does not contain verbatim lint_cmd trailing dot',
    (lintCmd, dir, _base) => {
      const result = buildScopedLintOneliner(lintCmd, dir, _base, 'check-tmpfile');
      expect(result).not.toBeNull();
      // Trailing '.' should be stripped — the file list drives scope
      expect(result).not.toMatch(/\s\.\s*$/);
      // Should reference the tmp file for that directory
      const slug = dir.replace(/\//g, '_');
      expect(result).toContain(`/tmp/check_${slug}_lint.txt`);
    },
  );

  it.each([
    ['ruff check .', 'svc', 'main'],
    ['python3 -m mypy', 'backend', 'develop'],
    ['flake8 .', 'src', 'main'],
    ['npx eslint .', 'admin-ui', 'main'],
    ['pyright .', 'agent', 'main'],
  ] as [string, string, string][])(
    'git-diff output for %s in dir=%s uses git diff pipeline against base branch',
    (lintCmd, dir, base) => {
      const result = buildScopedLintOneliner(lintCmd, dir, base, 'git-diff');
      expect(result).not.toBeNull();
      expect(result).toContain(`git diff origin/${base}...HEAD --name-only`);
      expect(result).toContain(`xargs -r`);
    },
  );

  it('ruff is rewritten to narrow ruleset for check-tmpfile', () => {
    const result = buildScopedLintOneliner('ruff check .', 'svc', 'main', 'check-tmpfile');
    expect(result).toContain('python3 -m ruff check --select E,F,W --ignore E501');
  });

  it('ruff is rewritten to narrow ruleset for git-diff', () => {
    const result = buildScopedLintOneliner('ruff check .', 'svc', 'main', 'git-diff');
    expect(result).toContain('python3 -m ruff check --select E,F,W --ignore E501');
  });

  it('check-tmpfile and git-diff produce semantically equivalent linter invocations for same lint_cmd', () => {
    const lintCmds = ['ruff check .', 'python3 -m mypy', 'flake8', 'npx eslint .'];
    for (const lintCmd of lintCmds) {
      const tmpfile = buildScopedLintOneliner(lintCmd, 'svc', 'main', 'check-tmpfile');
      const gitdiff = buildScopedLintOneliner(lintCmd, 'svc', 'main', 'git-diff');
      expect(tmpfile).not.toBeNull();
      expect(gitdiff).not.toBeNull();
      // Both must end with the same effective linter invocation (neither running verbatim full tree)
      expect(tmpfile).not.toMatch(/\s\.\s*$|\/\.\s*$/);
      expect(gitdiff).not.toMatch(/\s\.\s*$|\/\.\s*$/);
    }
  });

  it('python linters filter .py files for check-tmpfile', () => {
    for (const cmd of ['python3 -m mypy .', 'flake8 .', 'pyright .']) {
      const result = buildScopedLintOneliner(cmd, 'svc', 'main', 'git-diff');
      expect(result).toContain('\\.py');
      expect(result).not.toContain('ts|tsx');
    }
  });

  it('eslint filters JS/TS files for git-diff', () => {
    const result = buildScopedLintOneliner('npx eslint .', 'admin-ui', 'main', 'git-diff');
    expect(result).toContain('\\.(ts|tsx|js|jsx|cjs|mjs)');
    expect(result).not.toContain('\\.py');
  });

  it('strips trailing dot from mypy for check-tmpfile', () => {
    const result = buildScopedLintOneliner('python3 -m mypy .', 'svc', 'main', 'check-tmpfile');
    expect(result).not.toBeNull();
    // The xargs invocation should use 'python3 -m mypy' without trailing dot
    expect(result).toContain('python3 -m mypy');
    expect(result).not.toMatch(/mypy \. /);
  });

  it('strips trailing ./ from lint_cmd', () => {
    const result = buildScopedLintOneliner('flake8 ./', 'svc', 'main', 'check-tmpfile');
    expect(result).not.toBeNull();
    expect(result).not.toContain('./');
  });

  it('check-tmpfile output snapshot for ruff', () => {
    const result = buildScopedLintOneliner('ruff check .', 'svc', 'main', 'check-tmpfile');
    expect(result).toBe('xargs -0 -a /tmp/check_svc_lint.txt python3 -m ruff check --select E,F,W --ignore E501 2>&1');
  });

  it('git-diff output snapshot for ruff', () => {
    const result = buildScopedLintOneliner('ruff check .', 'svc', 'main', 'git-diff');
    expect(result).toBe("git diff origin/main...HEAD --name-only | grep -E '\\.py$' | sed 's|svc/||' | xargs -r python3 -m ruff check --select E,F,W --ignore E501");
  });

  it('git-diff output snapshot for mypy', () => {
    const result = buildScopedLintOneliner('python3 -m mypy', 'backend', 'main', 'git-diff');
    expect(result).toBe("git diff origin/main...HEAD --name-only | grep -E '\\.py$' | sed 's|backend/||' | xargs -r python3 -m mypy");
  });

  it('git-diff output snapshot for eslint', () => {
    const result = buildScopedLintOneliner('npx eslint .', 'admin-ui', 'main', 'git-diff');
    expect(result).toBe("git diff origin/main...HEAD --name-only | grep -E '\\.(ts|tsx|js|jsx|cjs|mjs)$' | sed 's|admin-ui/||' | xargs -r npx eslint");
  });
});

// ── buildConfigDrivenStackBlocks — scoped lint ─────────────────────────────
describe('writeCheckHelper — config-driven scoped lint', () => {
  let capturedScripts: Record<string, string> = {};

  const baseCfg = {
    taskId: 'task-lint-scope-1',
    workBranch: 'blueprint/task-lint-scope-1',
    baseBranch: 'main',
    owner: 'acme',
    repo: 'my-svc',
    testsOnMainBranch: false,
  };

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('scopes ruff check to changed .py files and uses narrow ruleset', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      ...baseCfg,
      buildConfig: {
        directories: [{ path: 'svc', type: 'python', lint_cmd: 'ruff check .' }],
      },
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    // Scoped block: FULL_SCOPE=1 branch has verbatim, else branch has scoped.
    expect(checkScript).toContain('if [ "$FULL_SCOPE" = "1" ]; then');
    expect(checkScript).toContain('ruff check --select E,F,W --ignore E501');
    expect(checkScript).toContain(`grep -E '^svc/.*\\.py$'`);
    // The verbatim form appears inside the FULL_SCOPE branch (not the default path).
    expect(checkScript).toContain(`run "svc lint" bash -c 'cd "svc" && ruff check .'`);
  });

  it('scopes eslint check to changed .ts files and keeps the FULL_SCOPE escape hatch', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      ...baseCfg,
      buildConfig: {
        directories: [{ path: 'svc', type: 'node', lint_cmd: 'npx eslint .' }],
      },
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toContain('if [ "$FULL_SCOPE" = "1" ]; then');
    expect(checkScript).toContain(`grep -E '^svc/.*\\.(ts|tsx|js|jsx|cjs|mjs)$'`);
    expect(checkScript).toContain(`run "svc lint" bash -c 'cd "svc" && npx eslint .'`);
  });

  it('keeps verbatim invocation for unrecognized lint_cmd without adding FULL_SCOPE block', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      ...baseCfg,
      buildConfig: {
        directories: [{ path: 'svc', type: 'python', lint_cmd: 'bash custom-lint.sh' }],
      },
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toContain(`run "svc lint" bash -c 'cd "svc" && bash custom-lint.sh'`);
    // No per-lint FULL_SCOPE conditional — verbatim commands run as-is.
    expect(checkScript).not.toContain('if [ "$FULL_SCOPE" = "1" ]; then');
  });
});
