import { vi, describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
  getAgentToken: vi.fn().mockReturnValue(''),
  git: vi.fn().mockReturnValue(''),
}));

// readFileSync is intentionally not fully mocked — the extracted .sh file
// lazy-loaders (check_*.sh) in check-helper.ts need to read real files from
// disk. writeFileSync remains mocked so we can capture generated scripts.
vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    default: {
      existsSync: vi.fn().mockReturnValue(false),
      readFileSync: actual.readFileSync,
      writeFileSync: vi.fn(),
      appendFileSync: vi.fn(),
      mkdirSync: vi.fn(),
      readdirSync: vi.fn().mockReturnValue([]),
      unlinkSync: vi.fn(),
    },
  };
});

// ── Imports (after mocks) ─────────────────────────────────────────────────────
import {
  detectLinterKind,
  buildScopedLintOneliner,
  sanitizeCiFailureSelectors,
} from './code-change-helpers.js';
import { prepareLifecycleHelperTestRepo } from './lifecycle-helper-test-repo.js';
// Some tests below write a generated bash helper with mode 0o755 and execute it
// (via a shebang/PATH-resolved curl stub), then parse its output. That requires
// a Unix executable bit, which Windows lacks — there the subprocess yields empty
// output. Gate those tests on this capability so they SKIP locally on Windows
// while still running fully in Linux CI.
const SUPPORTS_EXEC_BIT = process.platform !== 'win32';

describe('writeCodeChangeHelpers — self-review fallback', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
      // Self-repo runs write the generated check body to the gitignored
      // `.check-task` (the tracked `check` is a static delegating entrypoint —
      // see writeCheckHelper / check-helper.test.ts). These content assertions
      // are repo-agnostic, so mirror it under the `check` key the tests read.
      if (path.endsWith('/.check-task')) {
        capturedScripts[path.replace(/\.check-task$/, 'check')] = content;
      }
    });
  });

  it('provisions read-only compute-image status access in code-task workspaces', async () => {
    const previousProjectId = process.env.PROJECT_ID;
    process.env.PROJECT_ID = 'project-code-helper';
    try {
      const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
      writeCodeChangeHelpers({
        taskId: 'test-task-compute-image',
        workBranch: 'blueprint/test-task-compute-image',
        baseBranch: 'main',
        owner: 'example',
        repo: 'project',
        testsOnMainBranch: true,
      });
    } finally {
      if (previousProjectId === undefined) delete process.env.PROJECT_ID;
      else process.env.PROJECT_ID = previousProjectId;
    }

    const helper = capturedScripts['/tmp/workspace/compute-image'];
    expect(helper).toContain('/compute-environment');
    expect(helper).toContain("PROJECT='project-code-helper'");
    expect(helper).toContain('The backend enforces the Strategic-only build authority');
  });

  it('generates ./check with the admin-ui viewport gate and no jsdom test command', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-123',
      workBranch: 'blueprint/test-task-123',
      baseBranch: 'main',
      // Blueprint admin-ui/agent/backend blocks now apply only to the self-repo.
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
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
    expect(checkScript.indexOf('admin-ui viewport')).toBeLessThan(checkScript.indexOf('admin-ui typecheck'));
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
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
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
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
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
      // The migration-chain guard lives in the Blueprint admin-ui/agent/backend
      // blocks, which now apply only to the Blueprint self-repo.
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
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

    // Existing-PR path: PATCH call updates PR body (SR + screenshots sections)
    expect(prScript).toContain('PATCH');
    expect(prScript).toContain('/pulls/42');
    expect(prScript).toContain('PR body update');
  });
  it('generates ./check and ./pr with test gate when promotionTarget matches baseBranch', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-promo',
      workBranch: 'blueprint/test-task-promo',
      baseBranch: 'production',
      // Backend test-gate lives in the Blueprint blocks (self-repo only).
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
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

  it('self-repo keeps the test gate even when promotionTarget does not match baseBranch', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId: 'test-task-feat',
      workBranch: 'blueprint/test-task-feat',
      baseBranch: 'feature/xyz',
      // Backend test-gate condition lives in the Blueprint blocks.
      // Use a non-self-repo owner/repo so shouldRunBackendMergeGate checks
      // baseBranch === promotionTarget (the self-repo always returns true).
      owner: 'acme-org',
      repo: 'test-repo',
      testsOnMainBranch: false,
      promotionTarget: 'production',
    });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    const prScript = capturedScripts['/tmp/workspace/pr'];
    // baseBranch !== promotionTarget for a non-self repo: shouldRunBackendMergeGate
    // returns false, so no backend test gate is emitted in the check script and
    // no mandatory test gate appears in the pr script.
    expect(checkScript).not.toContain('"0" = "1"');
    expect(checkScript).toContain('no build_config configured');
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

  it('does NOT throw on a shell-composed build_config command — it is skipped from ./check', async () => {
    // resolveBuildConfig now retains commands with shell operators verbatim so
    // the backend's tests_gated decision and the agent's no-CI gate agree on
    // whether the project has tests. Such a command cannot be single-quote
    // interpolated into ./check, so it is OMITTED rather than throwing — but a
    // sibling safe command in the same directory IS still emitted.
    const captured: Record<string, string> = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      captured[path] = content;
    });
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    expect(() => {
      writeCodeChangeHelpers({
        ...validCfg,
        buildConfig: {
          directories: [{
            path: 'svc',
            type: 'node',
            build_cmd: 'npm run build; rm -rf /',
            test_cmd: 'npm test',
          }],
        },
      });
    }).not.toThrow();
    const checkScript = captured['/tmp/workspace/check'];
    expect(checkScript).not.toContain('npm run build; rm -rf /');
    expect(checkScript).toContain('npm test');
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
      // Self-repo runs write the generated check body to the gitignored
      // `.check-task` (the tracked `check` is a static delegating entrypoint —
      // see writeCheckHelper / check-helper.test.ts). These content assertions
      // are repo-agnostic, so mirror it under the `check` key the tests read.
      if (path.endsWith('/.check-task')) {
        capturedScripts[path.replace(/\.check-task$/, 'check')] = content;
      }
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

  it('emits the generic no-op fallback (no Blueprint blocks) for a foreign repo with no buildConfig', async () => {
    // baseCfg is acme/other-repo — a foreign repo. With no build_config it must
    // NOT inherit Blueprint's admin-ui/agent/backend checks; it gets the safe
    // generic fallback so we don't run the wrong commands against an unknown layout.
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg });

    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    expect(checkScript).toContain('no build_config configured for acme/other-repo');
    expect(checkScript).not.toContain('if admin_ui_runtime_touched; then');
    expect(checkScript).not.toContain('run "agent tsc"');
    expect(checkScript).not.toContain('backend ruff (changed)');
    expect(checkScript).not.toContain('npm run test:viewport');
    // And the config-driven marker is absent.
    expect(checkScript).not.toContain('Config-driven checks');
  });

  it('falls back to hardcoded Blueprint blocks when buildConfig is undefined ON THE SELF-REPO', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg, owner: 'HenryHamster', repo: 'BlueprintAIStudio' });

    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    expect(checkScript).toContain('admin_ui_runtime_touched');
    expect(checkScript).toContain('run "agent tsc"');
    expect(checkScript).toContain('backend ruff (changed)');
    expect(checkScript).toContain('npm run test:viewport');
    // And the config-driven marker is absent.
    expect(checkScript).not.toContain('Config-driven checks');
  });

  it('falls back to hardcoded Blueprint blocks when buildConfig has empty directories ON THE SELF-REPO', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg, owner: 'HenryHamster', repo: 'BlueprintAIStudio', buildConfig: { directories: [] } });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toContain('admin_ui_runtime_touched');
    expect(checkScript).not.toContain('Config-driven checks');
  });

  it('emits the generic fallback when a foreign repo supplies buildConfig with empty directories', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...baseCfg, buildConfig: { directories: [] } });
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toContain('no build_config configured for acme/other-repo');
    expect(checkScript).not.toContain('if admin_ui_runtime_touched; then');
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
      // Self-repo runs write the generated check body to the gitignored
      // `.check-task` (the tracked `check` is a static delegating entrypoint —
      // see writeCheckHelper / check-helper.test.ts). These content assertions
      // are repo-agnostic, so mirror it under the `check` key the tests read.
      if (path.endsWith('/.check-task')) {
        capturedScripts[path.replace(/\.check-task$/, 'check')] = content;
      }
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

// ── writeCodeChangeHelpers — ./blocked structured NEED flags ──────────────────
describe('writeCodeChangeHelpers — ./blocked structured need', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
      // Self-repo runs write the generated check body to the gitignored
      // `.check-task` (the tracked `check` is a static delegating entrypoint —
      // see writeCheckHelper / check-helper.test.ts). These content assertions
      // are repo-agnostic, so mirror it under the `check` key the tests read.
      if (path.endsWith('/.check-task')) {
        capturedScripts[path.replace(/\.check-task$/, 'check')] = content;
      }
    });
  });

  let nextTaskSeq = 0;
  const cfg = {
    taskId: 'test-task-123',
    workBranch: 'blueprint/test-task-123',
    baseBranch: 'main',
    owner: 'test-owner',
    repo: 'test-repo',
    testsOnMainBranch: true,
  };

  // Run the generated ./blocked script against a stubbed `curl` that captures
  // the POSTed payload, then return the parsed JSON body. The stub lives in a
  // temp bin dir prepended to PATH so the real network is never touched. Each
  // run uses a UNIQUE taskId so the /tmp/blocked_<taskId> idempotency marker
  // (written after a successful POST) never short-circuits a sibling run.
  const runBlocked = async (
    args: string[],
  ): Promise<{ exit: number; payload: Record<string, unknown> | null }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');

    const taskId = `blocked-need-${process.pid}-${nextTaskSeq++}`;
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...cfg, taskId });
    try { realFs.unlinkSync(`/tmp/blocked_${taskId}`); } catch { /* ignore */ }

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'blocked-need-'));
    try {
      const scriptPath = realPath.join(dir, 'blocked');
      realFs.writeFileSync(scriptPath, capturedScripts['/tmp/workspace/blocked'], { mode: 0o755 });
      const bin = realPath.join(dir, 'bin');
      realFs.mkdirSync(bin);
      const payloadFile = realPath.join(dir, 'payload.json');
      // curl stub: drain --data-binary @- from stdin into payloadFile, emit 200.
      realFs.writeFileSync(
        realPath.join(bin, 'curl'),
        `#!/bin/bash\ncat > "${payloadFile}"\nprintf '200'\n`,
        { mode: 0o755 },
      );
      prepareLifecycleHelperTestRepo(dir, taskId);
      const env = {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        ORCHESTRATOR_URL: 'http://orchestrator.test',
        AGENT_API_TOKEN: 'tok',
      };
      let exit = 0;
      try {
        realCp.execFileSync('bash', [scriptPath, ...args], { stdio: 'pipe', env });
      } catch (err: unknown) {
        exit = (err as { status?: number }).status ?? -1;
      }
      const payload: Record<string, unknown> | null = realFs.existsSync(payloadFile)
        ? (JSON.parse(realFs.readFileSync(payloadFile, 'utf8')) as Record<string, unknown>)
        : null;
      return { exit, payload };
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
      for (const marker of [`/tmp/blocked_${taskId}`, `/tmp/base_commit_${taskId}`]) { try { realFs.unlinkSync(marker); } catch { /* ignore */ } }
    }
  };

  it('generated ./blocked script passes `bash -n` syntax check', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    const blocked = capturedScripts['/tmp/workspace/blocked'];
    expect(blocked).toBeDefined();
    expect(blocked).toContain('_json_str()');
    expect(blocked).toContain('PAYLOAD=$(printf');
    expect(blocked).not.toContain('PAYLOAD=$(jq -n');
    expect(blocked).toContain('--needs-task');
    expect(blocked).toContain('--needs-pr');
    expect(blocked).toContain('--needs-capability');

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/blocked_smoke_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, blocked);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });

  // needs exec bit: runs a generated bash helper + PATH-resolved curl stub (mode 0o755)
  it.skipIf(!SUPPORTS_EXEC_BIT)('emits a normalized need in the payload when --needs-task is given an exact ref', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    // A real task UUID is an exact ref → preserved as a kind=task need.
    const uuidRef = '123e4567-e89b-12d3-a456-426614174000';
    const viaUuid = await runBlocked(['waiting on foundation', '--needs-task', uuidRef]);
    expect(viaUuid.exit).toBe(0);
    expect(viaUuid.payload?.failure_category).toBe('BLOCKED_PRECONDITION');
    expect(viaUuid.payload?.logs).toBe('BLOCKER: waiting on foundation');
    expect(viaUuid.payload?.blocked_need).toEqual({ kind: 'task', ref: uuidRef });
    // A 16-char finding hash is also exact.
    const viaHash = await runBlocked(['waiting', '--needs-task', 'deadbeefcafe0001']);
    expect(viaHash.payload?.blocked_need).toEqual({ kind: 'task', ref: 'deadbeefcafe0001' });
  });

  // needs exec bit: runs a generated bash helper + PATH-resolved curl stub (mode 0o755)
  it.skipIf(!SUPPORTS_EXEC_BIT)('records a title-like --needs-task ref as a capability need (exact-task-ref policy)', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    // 't-abc123' is neither a task UUID nor a finding hash → recorded as a
    // capability need instead of stranded "missing task" demand.
    const { exit, payload } = await runBlocked(['waiting', '--needs-task', 't-abc123']);
    expect(exit).toBe(0);
    expect(payload?.blocked_need).toEqual({ kind: 'capability', ref: 't-abc123' });
  });

  // needs exec bit: runs a generated bash helper + PATH-resolved curl stub (mode 0o755)
  it.skipIf(!SUPPORTS_EXEC_BIT)('maps --needs-pr and --needs-capability to the right kind', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    const pr = await runBlocked(['need pr', '--needs-pr', '7910']);
    expect(pr.payload?.blocked_need).toEqual({ kind: 'pr', ref: '7910' });
    const cap = await runBlocked(['need cap', '--needs-capability', 'shared auth layer']);
    expect(cap.payload?.blocked_need).toEqual({ kind: 'capability', ref: 'shared auth layer' });
  });

  // needs exec bit: runs a generated bash helper + PATH-resolved curl stub (mode 0o755)
  it.skipIf(!SUPPORTS_EXEC_BIT)('absent-need path preserves the legacy payload shape', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    const { exit, payload } = await runBlocked(['just a reason']);
    expect(exit).toBe(0);
    expect(payload).not.toBeNull();
    expect(payload).not.toHaveProperty('blocked_need');
    expect(payload?.logs).toBe('BLOCKER: just a reason');
    expect(payload?.failure_category).toBe('BLOCKED_PRECONDITION');
  });
});

// ── sanitizeCiFailureSelectors ───────────────────────────────────────────────
// The ci_failure closed-loop gate interpolates these node-ids into the
// generated ./check bash AND passes them to scripts/test-backend.sh. The
// sanitizer is the injection-safety boundary (shell-safe pytest node-ids only)
// AND the path normalizer (the harness runs pytest from the backend root, so
// node-ids are backend-relative). Anything malformed is dropped (graceful
// degradation) — never a hard block on a weird selector.
describe('sanitizeCiFailureSelectors', () => {
  it('accepts well-formed pytest node-ids (with and without ::nodeid)', () => {
    expect(sanitizeCiFailureSelectors([
      'testing/test_strategic_spawning.py::test_x',
      'testing/test_release_qualifier.py',
    ])).toEqual([
      'testing/test_strategic_spawning.py::test_x',
      'testing/test_release_qualifier.py',
    ]);
  });

  it('strips a leading backend/ prefix (harness runs pytest from backend root)', () => {
    expect(sanitizeCiFailureSelectors(['backend/testing/test_models.py::test_create']))
      .toEqual(['testing/test_models.py::test_create']);
  });

  it('normalizes Windows backslashes to forward slashes', () => {
    expect(sanitizeCiFailureSelectors(['testing\\test_win.py::test_z']))
      .toEqual(['testing/test_win.py::test_z']);
  });

  it('preserves parametrized node-ids ([param] ids)', () => {
    expect(sanitizeCiFailureSelectors(['testing/test_a.py::test_b[case-1]']))
      .toEqual(['testing/test_a.py::test_b[case-1]']);
  });

  it('dedups (after backend/ stripping) and preserves first-seen order', () => {
    expect(sanitizeCiFailureSelectors([
      'testing/test_dup.py::t',
      'backend/testing/test_dup.py::t',
      'testing/test_other.py::t',
    ])).toEqual(['testing/test_dup.py::t', 'testing/test_other.py::t']);
  });

  it('drops shell-unsafe selectors (injection-safety boundary)', () => {
    expect(sanitizeCiFailureSelectors([
      'testing/test_ok.py::test_good',
      'evil; rm -rf /',
      'testing/test_x.py::test_y && echo pwned',
      'testing/test_x.py::test_y | cat',
      "testing/test_x.py::test_'quote",
      'testing/test_x.py::test_$(whoami)',
      '$(rm -rf /)',
    ])).toEqual(['testing/test_ok.py::test_good']);
  });

  it('drops non-testing/ paths (only the backend test tree is in scope)', () => {
    expect(sanitizeCiFailureSelectors([
      'app/main.py::test_not_in_testing',
      'src/foo.test.ts',
      'testing/test_real.py::test_ok',
    ])).toEqual(['testing/test_real.py::test_ok']);
  });

  it('returns [] for undefined / empty / all-blank input', () => {
    expect(sanitizeCiFailureSelectors(undefined)).toEqual([]);
    expect(sanitizeCiFailureSelectors([])).toEqual([]);
    expect(sanitizeCiFailureSelectors(['', '   ', '\t'])).toEqual([]);
  });

  it('skips non-string entries defensively', () => {
    // Callers are typed string[], but a malformed payload could carry holes.
    expect(sanitizeCiFailureSelectors([
      // @ts-expect-error intentional malformed entry
      null,
      'testing/test_ok.py::test_good',
      // @ts-expect-error intentional malformed entry
      42,
    ])).toEqual(['testing/test_ok.py::test_good']);
  });
});

// ── viewport screenshot upload: generated ./check evidence gate ──────────────
describe('writeCodeChangeHelpers — viewport upload block', () => {
  let capturedScripts: Record<string, string> = {};

  const selfRepoCfg = {
    taskId: 'task-vp-upload-123',
    workBranch: 'blueprint/task-vp-upload-123',
    baseBranch: 'main',
    owner: 'HenryHamster',
    repo: 'BlueprintAIStudio',
    testsOnMainBranch: true,
  };

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
      // Self-repo runs write the generated check body to the gitignored
      // `.check-task` (the tracked `check` is a static delegating entrypoint —
      // see writeCheckHelper / check-helper.test.ts). These content assertions
      // are repo-agnostic, so mirror it under the `check` key the tests read.
      if (path.endsWith('/.check-task')) {
        capturedScripts[path.replace(/\.check-task$/, 'check')] = content;
      }
    });
  });

  it('viewport upload block: POST endpoint is referenced in generated check script', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(selfRepoCfg);
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    expect(checkScript).toContain('/api/agent/viewport/failure-screenshot-upload-url');
  });

  it('viewport upload block: upload endpoint absent from --full failure path', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(selfRepoCfg);
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    // The --full branch outputs "FAILED (--full, no baseline recheck)" and adds to FAILED.
    // The upload endpoint must NOT appear in that branch.
    const fullFailIdx = checkScript.indexOf('FAILED (--full, no baseline recheck)');
    const prOnlyIdx = checkScript.indexOf('passes on $BASE_REF, fails on PR. Real regression.');
    expect(fullFailIdx).toBeGreaterThan(-1);
    expect(prOnlyIdx).toBeGreaterThan(-1);
    // Upload endpoint appears only AFTER the PR-only regression marker, never before it
    // (which would mean it's reachable from the --full path).
    const uploadIdx = checkScript.indexOf('/api/agent/viewport/failure-screenshot-upload-url');
    expect(uploadIdx).toBeGreaterThan(prOnlyIdx);
    expect(uploadIdx).toBeGreaterThan(fullFailIdx);
    // The --full branch uses FULL_SCOPE=1 guard: ensure the upload block is inside
    // the else (non-full) branch by checking it does not appear between the --full
    // marker and the PR-only marker.
    const blockBetweenFullAndPrOnly = checkScript.slice(fullFailIdx, prOnlyIdx);
    expect(blockBetweenFullAndPrOnly).not.toContain('/api/agent/viewport/failure-screenshot-upload-url');
  });

  it('viewport upload block: inside PR-only else, not in baseline-also-broken WARN path', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(selfRepoCfg);
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();
    // WARN path marker
    const warnIdx = checkScript.indexOf('WARN — also broken on $BASE_REF; not blocking PR');
    const prOnlyIdx = checkScript.indexOf('passes on $BASE_REF, fails on PR. Real regression.');
    const uploadIdx = checkScript.indexOf('/api/agent/viewport/failure-screenshot-upload-url');
    expect(warnIdx).toBeGreaterThan(-1);
    expect(prOnlyIdx).toBeGreaterThan(-1);
    expect(uploadIdx).toBeGreaterThan(-1);
    // Upload block appears after the PR-only marker, not inside the WARN block.
    // The WARN and PR-only markers are siblings in an if/else; upload is in else (after PR-only).
    expect(uploadIdx).toBeGreaterThan(prOnlyIdx);
    // Slice from WARN marker to PR-only marker — upload must NOT appear in that region.
    const warnRegion = checkScript.slice(warnIdx, prOnlyIdx);
    expect(warnRegion).not.toContain('/api/agent/viewport/failure-screenshot-upload-url');
  });

  it('generated ./check with upload block passes bash -n syntax check', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(selfRepoCfg);
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const tmp = `${realOs.tmpdir().replace(/\\/g, '/')}/check_vp_upload_syntax_${process.pid}_${Date.now()}.sh`;
    realFs.writeFileSync(tmp, checkScript);
    try {
      realCp.execFileSync('bash', ['-n', tmp], { stdio: 'pipe' });
    } finally {
      try { realFs.unlinkSync(tmp); } catch { /* ignore */ }
    }
  });

  // needs exec bit: runs the generated upload block + PATH-resolved curl stub (mode 0o755)
  it.skipIf(!SUPPORTS_EXEC_BIT)('viewport upload: stubbed curl captures POST only for PR-only regression', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(selfRepoCfg);
    const checkScript = capturedScripts['/tmp/workspace/check'];
    expect(checkScript).toBeDefined();

    // Extract the upload sub-block in isolation (start..end markers keep the
    // extracted block self-contained so extra fi/done from surrounding bash
    // structure don't cause syntax errors when run standalone).
    const startMarker = '# ── viewport screenshot upload (PR-only regression evidence) ──────────';
    const endMarker = '# ── end viewport screenshot upload ───────────────────────────────────';
    const startIdx = checkScript.indexOf(startMarker);
    expect(startIdx).toBeGreaterThan(-1);
    const endIdx = checkScript.indexOf(endMarker, startIdx);
    expect(endIdx).toBeGreaterThan(startIdx);
    const uploadBlock = checkScript.slice(startIdx, endIdx + endMarker.length);

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');

    // Build a stubbed environment: fake viewport-failures.json + PNGs + a curl stub.
    const runUploadBlock = (
      vpJson: string,
      pngNames: string[],
    ): { exit: number; postLog: string[]; putLog: string[] } => {
      const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'vp-upload-'));
      try {
        // Write fake test-results structure.
        realFs.mkdirSync(realPath.join(dir, 'admin-ui', 'test-results'), { recursive: true });
        realFs.writeFileSync(realPath.join(dir, 'admin-ui', 'test-results', 'viewport-failures.json'), vpJson);
        for (const pngName of pngNames) {
          realFs.writeFileSync(realPath.join(dir, 'admin-ui', 'test-results', pngName), 'PNG');
        }
        // Stub curl: record POST payloads and PUT sources, return a fake signed URL response.
        const postLog = realPath.join(dir, 'curl_post.log');
        const putLog = realPath.join(dir, 'curl_put.log');
        const bin = realPath.join(dir, 'bin');
        realFs.mkdirSync(bin);
        realFs.writeFileSync(
          realPath.join(bin, 'curl'),
          [
            '#!/bin/bash',
            'method=""',
            'prev=""',
            'for a in "$@"; do',
            '  if [ "$a" = "POST" ]; then',
            '    method="POST"',
            '  fi',
            '  if [ "$prev" = "-T" ]; then',
            `    echo "PUT $(basename "$a")" >> "${putLog}"`,
            '  fi',
            '  if [ "$prev" = "-d" ]; then',
            `    echo "POST $a" >> "${postLog}"`,
            '  fi',
            '  prev="$a"',
            'done',
            'if [ "$method" = "POST" ]; then',
            `  printf '{"upload_url":"http://fake-gcs/upload","storage_uri":"gs://bucket/vp.png"}'`,
            '  exit 0',
            'fi',
            // PUT: just succeed silently.
            'exit 0',
          ].join('\n'),
          { mode: 0o755 },
        );
        // Extract the _load_agent_token function and the _json_field helper from the
        // full check script header so the isolated upload block can call them
        // (the block guard uses them for field extraction and token loading).
        const fnStart = checkScript.indexOf('_load_agent_token()');
        const fnEnd = checkScript.indexOf('\n}', fnStart) + 2;
        const loadTokenFn = fnStart >= 0 ? checkScript.slice(fnStart, fnEnd) : '';
        const jfStart = checkScript.indexOf('_json_field()');
        const jfEnd = checkScript.indexOf('\n}', jfStart) + 2;
        const jsonFieldFn = jfStart >= 0 ? checkScript.slice(jfStart, jfEnd) : '';
        const harness = [
          'set -uo pipefail',
          loadTokenFn,
          jsonFieldFn,
          `cd "${dir}"`,
          uploadBlock,
        ].join('\n');
        const harnessFile = realPath.join(dir, 'harness.sh');
        realFs.writeFileSync(harnessFile, harness);
        const env = {
          ...process.env,
          PATH: `${bin}:${process.env.PATH}`,
          ORCHESTRATOR_URL: 'http://orchestrator.test',
          AGENT_API_TOKEN: 'tok',
        };
        let exit = 0;
        try {
          realCp.execFileSync('bash', [harnessFile], { stdio: 'pipe', env });
        } catch (err: unknown) {
          exit = (err as { status?: number }).status ?? -1;
        }
        const postPayloads = realFs.existsSync(postLog)
          ? realFs.readFileSync(postLog, 'utf8').trim().split('\n').filter(Boolean)
          : [];
        const putPayloads = realFs.existsSync(putLog)
          ? realFs.readFileSync(putLog, 'utf8').trim().split('\n').filter(Boolean)
          : [];
        return { exit, postLog: postPayloads, putLog: putPayloads };
      } finally {
        realFs.rmSync(dir, { recursive: true, force: true });
      }
    };

    // With two failed tests and matching PNGs: each POST should use the right
    // component and each PUT should target the matching screenshot.
    const failedJson = JSON.stringify({
      testResults: [
        {
          name: 'src/components/NewTaskModal.viewport.test.tsx',
          assertionResults: [{
            status: 'failed',
            fullName: 'NewTaskModal — viewport regression renders without overflow at 375px (mobile)',
          }],
        },
        {
          name: 'src/components/MessagesPanel.viewport.test.tsx',
          assertionResults: [{
            status: 'failed',
            fullName: 'MessagesPanel composed with shell panels — viewport regression renders without overflow at 768px (tablet)',
          }],
        },
      ],
    });
    const withFailure = runUploadBlock(failedJson, [
      'NewTaskModal-viewport-regression-renders-without-overflow-at-375px-mobile.png',
      'MessagesPanel-composed-with-shell-panels-viewport-regression-renders-without-overflow-at-768px-tablet.png',
      'screenshot.png',
    ]);
    expect(withFailure.exit).toBe(0);
    expect(withFailure.postLog).toEqual([
      'POST {"task_id":"task-vp-upload-123","component":"NewTaskModal","width":375}',
      'POST {"task_id":"task-vp-upload-123","component":"MessagesPanel","width":768}',
    ]);
    expect(withFailure.putLog).toEqual([
      'PUT NewTaskModal-viewport-regression-renders-without-overflow-at-375px-mobile.png',
      'PUT MessagesPanel-composed-with-shell-panels-viewport-regression-renders-without-overflow-at-768px-tablet.png',
    ]);

    // With all tests passing: no POST should be attempted.
    const passingJson = JSON.stringify({
      testResults: [{
        assertionResults: [{
          status: 'passed',
          fullName: 'EffortEfficiencyPanel viewport renders without overflow at 375px (mobile)',
        }],
      }],
    });
    const withPassing = runUploadBlock(passingJson, []);
    expect(withPassing.exit).toBe(0);
    expect(withPassing.postLog).toEqual([]);
    expect(withPassing.putLog).toEqual([]);
  });
});

// ── generated lifecycle helpers prefer AGENT_TOKEN_FILE over stale AGENT_API_TOKEN ──
describe('writeCodeChangeHelpers — lifecycle helpers use live token file', () => {
  let capturedScripts: Record<string, string> = {};
  let nextSeq = 0;

  const cfg = {
    taskId: 'task-token-test',
    workBranch: 'blueprint/task-token-test',
    baseBranch: 'main',
    owner: 'test-owner',
    repo: 'test-repo',
    testsOnMainBranch: false,
  };

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
      if (typeof path === 'string' && path.endsWith('/.check-task')) {
        capturedScripts[path.replace(/\.check-task$/, 'check')] = content;
      }
    });
  });

  it('generated ./fail embeds _load_agent_token and does not hard-bind from AGENT_API_TOKEN alone', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    const failScript = capturedScripts['/tmp/workspace/fail'];
    expect(failScript).toBeDefined();
    expect(failScript).toContain('_load_agent_token');
    expect(failScript).toContain('_load_agent_token()');
    expect(failScript).toContain('_json_str()');
    expect(failScript).toContain('PAYLOAD=$(printf');
    expect(failScript).not.toContain('PAYLOAD=$(jq -n');
    // Must not use the stale env snapshot as the ONLY token source.
    expect(failScript).not.toContain('TOKEN="${AGENT_API_TOKEN:?');
    // Env fallback must still exist inside the function (one-shot agents).
    expect(failScript).toContain('AGENT_API_TOKEN');
    expect(failScript).toContain('blueprint-checkpoint: preserve unshipped task task-token-test');
    expect(failScript).toContain('_CHECKPOINT_LEASE="--force-with-lease=refs/heads/blueprint/task-token-test:$_CHECKPOINT_EXPECTED_SHA"');
    expect(failScript).toContain('push "$_CHECKPOINT_LEASE" origin');
    expect(failScript.indexOf('_checkpoint_terminal_work ||')).toBeLessThan(
      failScript.indexOf('-X POST "$API/api/agent/status"'),
    );
  });

  it('generated ./blocked embeds _load_agent_token and does not hard-bind from AGENT_API_TOKEN alone', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    const blockedScript = capturedScripts['/tmp/workspace/blocked'];
    expect(blockedScript).toBeDefined();
    expect(blockedScript).toContain('_load_agent_token');
    expect(blockedScript).toContain('_load_agent_token()');
    expect(blockedScript).toContain('_json_str()');
    expect(blockedScript).toContain('PAYLOAD=$(printf');
    expect(blockedScript).not.toContain('PAYLOAD=$(jq -n');
    expect(blockedScript).not.toContain('TOKEN="${AGENT_API_TOKEN:?');
    expect(blockedScript).toContain('AGENT_API_TOKEN');
    expect(blockedScript).toContain('Blueprint-Base-Commit: $_CHECKPOINT_BASE');
    expect(blockedScript).toContain('_CHECKPOINT_LEASE="--force-with-lease=refs/heads/blueprint/task-token-test:$_CHECKPOINT_EXPECTED_SHA"');
    expect(blockedScript).toContain('push "$_CHECKPOINT_LEASE" origin');
    expect(blockedScript.indexOf('_checkpoint_terminal_work ||')).toBeLessThan(
      blockedScript.indexOf('-X POST "$API/api/agent/status"'),
    );
  });

  it('generated ./done embeds _load_agent_token and does not hard-bind from AGENT_API_TOKEN alone', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    const doneScript = capturedScripts['/tmp/workspace/done'];
    expect(doneScript).toBeDefined();
    expect(doneScript).toContain('_load_agent_token');
    expect(doneScript).toContain('_load_agent_token()');
    expect(doneScript).not.toContain('TOKEN="${AGENT_API_TOKEN:?');
    expect(doneScript).toContain('AGENT_API_TOKEN');
  });

  it('generated ./pr embeds _load_agent_token and does not hard-bind from AGENT_API_TOKEN alone', async () => {
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers(cfg);
    const prScript = capturedScripts['/tmp/workspace/pr'];
    expect(prScript).toBeDefined();
    expect(prScript).toContain('_load_agent_token');
    expect(prScript).toContain('_load_agent_token()');
    expect(prScript).not.toContain('TOKEN="${AGENT_API_TOKEN:?');
    expect(prScript).toContain('AGENT_API_TOKEN');
  });

  // needs exec bit: runs the generated ./fail with a PATH-resolved curl stub and
  // a live AGENT_TOKEN_FILE to prove the helper reads the FILE token, not the
  // stale AGENT_API_TOKEN env snapshot.
  it.skipIf(!SUPPORTS_EXEC_BIT)('generated ./fail uses AGENT_TOKEN_FILE token over stale AGENT_API_TOKEN for the orchestrator call', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');

    const taskId = `fail-live-token-${process.pid}-${nextSeq++}`;
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...cfg, taskId });
    try { realFs.unlinkSync(`/tmp/fail_${taskId}`); } catch { /* ignore */ }

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'fail-live-tok-'));
    try {
      const scriptPath = realPath.join(dir, 'fail');
      realFs.writeFileSync(scriptPath, capturedScripts['/tmp/workspace/fail'], { mode: 0o755 });

      // Write the live token to a file — this is what the agent process keeps fresh.
      // Use a JWT-shaped value (header.payload.sig) so it passes the loader's
      // _agent_token_looks_like_jwt gate instead of being rejected as a placeholder.
      const tokenFile = realPath.join(dir, 'agent-token.jwt');
      realFs.writeFileSync(tokenFile, 'live.file.token', { mode: 0o600 });

      // curl stub: capture the X-Agent-Token header value used in the request.
      const bin = realPath.join(dir, 'bin');
      realFs.mkdirSync(bin);
      const headerLog = realPath.join(dir, 'header.log');
      realFs.writeFileSync(
        realPath.join(bin, 'curl'),
        [
          '#!/bin/bash',
          'prev=""',
          'for a in "$@"; do',
          '  if [ "$prev" = "-H" ] || [ "$prev" = "--header" ]; then',
          `    echo "$a" >> "${headerLog}"`,
          '  fi',
          '  prev="$a"',
          'done',
          // Drain stdin (the payload), emit a 200 status, exit 0.
          'cat > /dev/null',
          'printf "200"',
          'exit 0',
        ].join('\n'),
        { mode: 0o755 },
      );
      prepareLifecycleHelperTestRepo(dir, taskId);

      // Run with a STALE AGENT_API_TOKEN and a fresh AGENT_TOKEN_FILE.
      // The helper must use the file token, not the stale env snapshot.
      const env: Record<string, string> = {
        ...Object.fromEntries(Object.entries(process.env).filter(([, v]) => v !== undefined)) as Record<string, string>,
        PATH: `${bin}:${process.env.PATH ?? ''}`,
        ORCHESTRATOR_URL: 'http://orchestrator.test',
        AGENT_API_TOKEN: 'stale-env-token',
        AGENT_TOKEN_FILE: tokenFile,
      };
      try {
        realCp.execFileSync('bash', [scriptPath, 'test reason'], { stdio: 'pipe', env });
      } catch { /* exit code from idempotency re-run is fine */ }

      // The X-Agent-Token header must carry the FILE token, not the stale env snapshot.
      const headers = realFs.existsSync(headerLog) ? realFs.readFileSync(headerLog, 'utf8') : '';
      const agentTokenHeader = headers.split('\n').find(h => h.startsWith('X-Agent-Token:')) ?? '';
      expect(agentTokenHeader).toContain('live.file.token');
      expect(agentTokenHeader).not.toContain('stale-env-token');
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
      for (const marker of [`/tmp/fail_${taskId}`, `/tmp/base_commit_${taskId}`]) { try { realFs.unlinkSync(marker); } catch { /* ignore */ } }
    }
  });
});

// ── ./fail and ./blocked build JSON without jq ────────────────────────────────
// The strategist VM class does not reliably ship `jq`. These helpers used to
// build their status payloads with `jq -n --arg ...`, so the self-failure and
// precondition-blocked write paths broke before the HTTP call on a jq-less VM.
// They now build the body with the portable `_json_str` encoder + a printf
// skeleton. A "poison" jq at the FRONT of PATH records a marker if invoked, so a
// genuinely jq-free path is proven by the marker's ABSENCE.
describe('writeCodeChangeHelpers — jq-free status payload build', () => {
  let capturedScripts: Record<string, string> = {};
  let nextSeq = 0;

  const cfg = {
    taskId: 'task-nojq',
    workBranch: 'blueprint/task-nojq',
    baseBranch: 'main',
    owner: 'test-owner',
    repo: 'test-repo',
    testsOnMainBranch: false,
  };

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
      if (typeof path === 'string' && path.endsWith('/.check-task')) {
        capturedScripts[path.replace(/\.check-task$/, 'check')] = content;
      }
    });
  });

  // Generate the helpers, run `helper` (fail|blocked) with jq poisoned + a curl
  // stub that captures the POST body, and return the parsed payload.
  const runNoJq = async (
    helper: 'fail' | 'blocked',
    args: string[],
  ): Promise<{ exit: number; jqCalled: boolean; payload: Record<string, unknown> | null }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');

    const taskId = `nojq-${helper}-${process.pid}-${nextSeq++}`;
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({ ...cfg, taskId });
    try { realFs.unlinkSync(`/tmp/${helper}_${taskId}`); } catch { /* ignore */ }

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), `nojq-${helper}-`));
    try {
      const scriptPath = realPath.join(dir, helper);
      realFs.writeFileSync(scriptPath, capturedScripts[`/tmp/workspace/${helper}`], { mode: 0o755 });
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
      prepareLifecycleHelperTestRepo(dir, taskId);
      const env = {
        ...process.env,
        PATH: `${bin}:${process.env.PATH}`,
        ORCHESTRATOR_URL: 'http://orchestrator.test',
        AGENT_API_TOKEN: 'tok',
      };
      let exit = 0;
      try {
        realCp.execFileSync('bash', [scriptPath, ...args], { stdio: 'pipe', env });
      } catch (err: unknown) {
        exit = (err as { status?: number }).status ?? -1;
      }
      const jqCalled = realFs.existsSync(jqMarker);
      const payload: Record<string, unknown> | null = realFs.existsSync(payloadFile)
        ? (JSON.parse(realFs.readFileSync(payloadFile, 'utf8')) as Record<string, unknown>)
        : null;
      return { exit, jqCalled, payload };
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
      for (const marker of [`/tmp/${helper}_${taskId}`, `/tmp/base_commit_${taskId}`]) { try { realFs.unlinkSync(marker); } catch { /* ignore */ } }
    }
  };

  it.skipIf(!SUPPORTS_EXEC_BIT)('./fail builds a valid self-failure payload with jq REMOVED from PATH (quotes + newline + unicode)', async () => {
    // Reason mixes an apostrophe, embedded double-quotes, a newline and multibyte
    // unicode — exercises every _json_str escape branch.
    const reason = "scope's wrong: expected \"X\"\nbut got café";
    const { exit, jqCalled, payload } = await runNoJq('fail', [reason]);
    expect(exit).toBe(0);
    expect(jqCalled).toBe(false);
    expect(payload).not.toBeNull();
    expect(payload?.status).toBe('FAILED');
    expect(payload?.artifact_type).toBe('ERROR_LOG');
    expect(payload?.failure_category).toBe('agent_self_failed');
    expect(payload?.logs).toBe(reason);
  });

  it.skipIf(!SUPPORTS_EXEC_BIT)('./blocked (no need) builds the legacy payload shape with jq REMOVED from PATH', async () => {
    const { exit, jqCalled, payload } = await runNoJq('blocked', ["can't proceed: \"blocked\""]);
    expect(exit).toBe(0);
    expect(jqCalled).toBe(false);
    expect(payload).not.toBeNull();
    expect(payload?.failure_category).toBe('BLOCKED_PRECONDITION');
    expect(payload?.logs).toBe('BLOCKER: can\'t proceed: "blocked"');
    expect(payload).not.toHaveProperty('blocked_need');
  });

  it.skipIf(!SUPPORTS_EXEC_BIT)('./blocked (--needs-pr) includes the blocked_need object with jq REMOVED from PATH', async () => {
    const { exit, jqCalled, payload } = await runNoJq('blocked', ['waiting on merge', '--needs-pr', '7910']);
    expect(exit).toBe(0);
    expect(jqCalled).toBe(false);
    expect(payload?.logs).toBe('BLOCKER: waiting on merge');
    expect(payload?.blocked_need).toEqual({ kind: 'pr', ref: '7910' });
  });
});

describe('writeCodeChangeHelpers — executable terminal checkpoint lease', () => {
  let capturedScripts: Record<string, string> = {};
  let sequence = 0;

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
      if (typeof path === 'string' && path.endsWith('/.check-task')) {
        capturedScripts[path.replace(/\.check-task$/, 'check')] = content;
      }
    });
  });

  type PushMode = 'normal' | 'lost-response' | 'competing' | 'remote-unavailable'; type RealFixture = {
    baseCommit: string;
    bare: string;
    dir: string;
    helperPath: string;
    postLog: string;
    repo: string;
    taskId: string;
    workBranch: string;
    bash: string;
    git: string;
    env: NodeJS.ProcessEnv;
  };

  const slash = (value: string): string => value.replace(/\\/g, '/');

  async function makeFixture(helper: 'fail' | 'blocked', mode: PushMode): Promise<RealFixture> {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const bash = process.platform === 'win32' ? 'C:/Program Files/Git/bin/bash.exe' : 'bash';
    const git = process.platform === 'win32' ? 'C:/Program Files/Git/cmd/git.exe' : 'git';
    const taskId = `checkpoint-${helper}-${mode}-${process.pid}-${sequence++}`;
    const workBranch = `blueprint/${taskId}`;
    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'terminal-checkpoint-'));
    const bare = realPath.join(dir, 'remote.git');
    const repo = realPath.join(dir, 'work');
    const bin = realPath.join(dir, 'bin');
    const postLog = realPath.join(dir, 'status-post.log');
    const shimState = realPath.join(dir, 'push-shim-fired');
    realFs.mkdirSync(repo);
    realFs.mkdirSync(bin);

    const runGit = (cwd: string, args: string[]): string => realCp.execFileSync(
      git,
      args,
      { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
    ).trim();
    runGit(dir, ['init', '--bare', bare]);
    runGit(repo, ['init']);
    runGit(repo, ['config', 'user.name', 'Checkpoint Test']);
    runGit(repo, ['config', 'user.email', 'checkpoint@example.test']);
    runGit(repo, ['checkout', '-b', 'main']);
    realFs.writeFileSync(realPath.join(repo, 'work.txt'), 'base\n');
    runGit(repo, ['add', 'work.txt']);
    runGit(repo, ['commit', '-m', 'base']);
    const baseCommit = runGit(repo, ['rev-parse', 'HEAD']);
    runGit(repo, ['remote', 'add', 'origin', slash(bare)]);
    runGit(repo, ['push', '-u', 'origin', 'main']);

    let competingSha = '';
    if (mode === 'competing') {
      runGit(repo, ['checkout', '-b', 'competitor-seed']);
      realFs.writeFileSync(realPath.join(repo, 'competitor.txt'), 'do not overwrite\n');
      runGit(repo, ['add', 'competitor.txt']);
      runGit(repo, ['commit', '-m', 'competing checkpoint owner']);
      competingSha = runGit(repo, ['rev-parse', 'HEAD']);
      runGit(repo, ['push', 'origin', `${competingSha}:refs/heads/competitor-seed`]);
      runGit(repo, ['checkout', 'main']);
    }
    realFs.writeFileSync(realPath.join(repo, 'work.txt'), `base\npending ${helper} work\n`);

    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId,
      workBranch,
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
    });
    const helperPath = realPath.join(repo, helper);
    realFs.writeFileSync(helperPath, capturedScripts[`/tmp/workspace/${helper}`], { mode: 0o755 });

    const gitShim = `#!/bin/bash
for _arg in "$@"; do
  if [ "$_arg" = "ls-remote" ] && [ "$CHECKPOINT_MODE" = "remote-unavailable" ]; then
    exit 74
  fi
  if [ "$_arg" = "push" ]; then
    if [ "$CHECKPOINT_MODE" = "lost-response" ] && [ ! -e "$SHIM_STATE" ]; then
      : > "$SHIM_STATE"
      "$REAL_GIT" "$@" || exit $?
      exit 75
    fi
    if [ "$CHECKPOINT_MODE" = "competing" ] && [ ! -e "$SHIM_STATE" ]; then
      : > "$SHIM_STATE"
      "$REAL_GIT" --git-dir="$BARE_REMOTE" update-ref \
        "refs/heads/$WORK_BRANCH" "$COMPETING_SHA" || exit $?
      exit 75
    fi
  fi
done
exec "$REAL_GIT" "$@"
`;
    realFs.writeFileSync(realPath.join(bin, 'git'), gitShim, { mode: 0o755 });
    realFs.writeFileSync(
      realPath.join(bin, 'node'),
      '#!/bin/bash\n_value=$(cat)\nprintf \'"%s"\' "$_value"\n',
      { mode: 0o755 },
    );
    realFs.writeFileSync(realPath.join(bin, 'sleep'), '#!/bin/bash\nexit 0\n', { mode: 0o755 });
    const bashEnv = realPath.join(dir, 'bash-env.sh');
    realFs.writeFileSync(
      bashEnv,
      `git() { "$CHECKPOINT_SHIM_BIN/git" "$@"; }
curl() { "$CHECKPOINT_SHIM_BIN/curl" "$@"; }
node() { "$CHECKPOINT_SHIM_BIN/node" "$@"; }
sleep() { return 0; }
`,
    );
    const curlShim = `#!/bin/bash
for _arg in "$@"; do
  case "$_arg" in
    */api/agent/github/token*) printf '%s' '{"data":{"token":""}}'; exit 0 ;;
  esac
done
_remote_sha=$("$REAL_GIT" --git-dir="$BARE_REMOTE" rev-parse \
  "refs/heads/$WORK_BRANCH" 2>/dev/null) || exit 76
printf '%s\n' "$_remote_sha" >> "$POST_LOG"
printf '200'
`;
    realFs.writeFileSync(realPath.join(bin, 'curl'), curlShim, { mode: 0o755 });

    const env = Object.fromEntries(Object.entries({
      ...process.env,
      ORCHESTRATOR_URL: 'http://orchestrator.test',
      AGENT_API_TOKEN: 'checkpoint-token',
      REAL_GIT: slash(git),
      BARE_REMOTE: slash(bare),
      WORK_BRANCH: workBranch,
      CHECKPOINT_MODE: mode,
      SHIM_STATE: slash(shimState),
      COMPETING_SHA: competingSha,
      POST_LOG: slash(postLog),
      CHECKPOINT_SHIM_BIN: slash(bin),
      BASH_ENV: slash(bashEnv),
    }).filter(([key]) => key.toLowerCase() !== 'path')) as NodeJS.ProcessEnv;
    env.PATH = (process.platform === 'win32'
      ? [bin, 'C:\\Program Files\\Git\\usr\\bin', 'C:\\Program Files\\Git\\bin']
      : [bin, '/usr/bin', '/bin']).join(realPath.delimiter);
    const baseMarker = `/tmp/base_commit_${taskId}`;
    realCp.execFileSync(
      bash,
      ['-c', 'printf %s "$1" > "$2"', 'checkpoint-test', baseCommit, baseMarker],
      { env, stdio: 'pipe' },
    );
    realCp.execFileSync(
      bash,
      ['-c', 'rm -f "$@"', 'checkpoint-test', `/tmp/${helper}_${taskId}`],
      { env, stdio: 'pipe' },
    );
    return { baseCommit, bare, dir, helperPath, postLog, repo, taskId, workBranch, bash, git, env };
  }

  async function disposeFixture(fixture: RealFixture): Promise<void> {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    try {
      realCp.execFileSync(
        fixture.bash,
        [
          '-c', 'rm -f "$@"', 'checkpoint-test',
          `/tmp/base_commit_${fixture.taskId}`,
          `/tmp/fail_${fixture.taskId}`,
          `/tmp/blocked_${fixture.taskId}`,
          `/tmp/checkpoint_commit_${fixture.taskId}.log`,
          `/tmp/checkpoint_push_${fixture.taskId}.log`,
          `/tmp/checkpoint_remote_${fixture.taskId}.log`,
        ],
        { env: fixture.env, stdio: 'pipe' },
      );
    } finally {
      realFs.rmSync(fixture.dir, { recursive: true, force: true });
    }
  }

  it('does not POST ./blocked terminal state when the initial lease cannot be read', async () => {
    const fixture = await makeFixture('blocked', 'remote-unavailable');
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    try {
      let exit = 0;
      try {
        realCp.execFileSync(
          fixture.bash,
          [fixture.helperPath, 'remote unavailable'],
          { cwd: fixture.repo, env: fixture.env, stdio: 'pipe' },
        );
      } catch (error: unknown) {
        exit = (error as { status?: number }).status ?? -1;
      }
      expect(exit).not.toBe(0);
      expect(realFs.existsSync(fixture.postLog)).toBe(false);
      expect(() => realCp.execFileSync(
        fixture.git,
        ['--git-dir', fixture.bare, 'rev-parse', `refs/heads/${fixture.workBranch}`],
        { stdio: 'pipe' },
      )).toThrow();
    } finally {
      await disposeFixture(fixture);
    }
  }, 30_000);
});
