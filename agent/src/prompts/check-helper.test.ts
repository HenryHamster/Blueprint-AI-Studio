import { vi, describe, it, expect, beforeEach } from 'vitest';
import fs from 'fs';
import type { DirectoryConfig } from '../utils/test-runners.js';

describe('tracked root check delegation contract', () => {
  it('prefers a generated gate and reserves the tracked default for true developer checkouts', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const script = realFs.readFileSync(realPath.resolve(process.cwd(), '..', 'check'), 'utf8');

    expect(script).toContain('TASK_GATE="$WORKSPACE/.check-task"');
    expect(script).toContain('DEFAULT_GATE="$WORKSPACE/scripts/check-default.sh"');
    expect(script).toContain('export WORKSPACE');
    expect(script).toContain('if [ -f "$TASK_GATE" ]; then');
    expect(script).toContain('CAPABILITY_RECEIPT="/tmp/execution_capability_receipt_${TASK_ID}"');
    expect(script).toContain('generated self-repo gate is missing');
    expect(script).toContain('generated self-repo gate is not executable');
    expect(script).toContain('exec "$TASK_GATE" "$@"');
    expect(script).toContain('exec "$DEFAULT_GATE" "$@"');
    expect(script).not.toContain('PASS — all checks green');
  });

  it('delegates by runtime kind and clears stale authority before a missing task gate fails', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const repo = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'root-check-wrapper-'));
    const bashWorkspace = realCp.execFileSync('bash', ['-lc', 'cygpath -u "$1" 2>/dev/null || printf %s "$1"', '_', repo], {
      encoding: 'utf8',
    }).trim();
    const taskGate = realPath.join(repo, '.check-task');
    const scriptsDir = realPath.join(repo, 'scripts');
    const defaultGate = realPath.join(scriptsDir, 'check-default.sh');
    const delegatedArgs = realPath.join(repo, 'delegated-args.txt');
    const bashTmp = realCp.execFileSync('bash', ['-lc', 'cygpath -w /tmp 2>/dev/null || printf /tmp'], {
      encoding: 'utf8',
    }).trim();
    const capabilityReceipt = realPath.join(bashTmp, 'execution_capability_receipt_wrapper-test');
    const checkPath = realPath.resolve(process.cwd(), '..', 'check');
    const fixtureCheck = realPath.join(repo, 'check');
    try {
      realFs.mkdirSync(scriptsDir);
      realFs.copyFileSync(checkPath, fixtureCheck);
      realFs.writeFileSync(taskGate, '#!/bin/bash\nprintf "task:%s\\n" "$@" > "$WORKSPACE/delegated-args.txt"\n');
      realFs.writeFileSync(defaultGate, '#!/bin/bash\nprintf "workspace:%s\\n" "$WORKSPACE" > "$WORKSPACE/delegated-args.txt"\nprintf "default:%s\\n" "$@" >> "$WORKSPACE/delegated-args.txt"\n');
      realFs.chmodSync(fixtureCheck, 0o755);
      realFs.chmodSync(taskGate, 0o755);
      realFs.chmodSync(defaultGate, 0o755);
      const unboundEnv = { ...process.env };
      delete unboundEnv.WORKSPACE;
      delete unboundEnv.TASK_ID;
      realCp.execFileSync('bash', [fixtureCheck, 'test', '--full'], {
        cwd: realOs.tmpdir(),
        env: unboundEnv,
        stdio: 'pipe',
      });
      expect(realFs.readFileSync(delegatedArgs, 'utf8')).toBe('task:test\ntask:--full\n');

      realFs.unlinkSync(taskGate);
      realFs.writeFileSync(capabilityReceipt, 'backend_verification\n');
      let missingStatus = 0;
      try {
        realCp.execFileSync('bash', [fixtureCheck], {
          env: { ...process.env, WORKSPACE: bashWorkspace, TASK_ID: 'wrapper-test' },
          stdio: 'pipe',
        });
      } catch (error) {
        missingStatus = (error as { status?: number }).status ?? -1;
      }
      expect(missingStatus).toBe(2);
      expect(realFs.existsSync(capabilityReceipt)).toBe(false);

      const developerEnv = { ...unboundEnv };
      realCp.execFileSync('bash', [fixtureCheck, 'test'], {
        cwd: realOs.tmpdir(),
        env: developerEnv,
        stdio: 'pipe',
      });
      expect(realFs.readFileSync(delegatedArgs, 'utf8')).toBe(`workspace:${bashWorkspace}\ndefault:test\n`);
    } finally {
      realFs.rmSync(repo, { recursive: true, force: true });
      try { realFs.unlinkSync(capabilityReceipt); } catch { /* absent */ }
    }
  });
});

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

// readFileSync is intentionally not fully mocked — the extracted .sh file
// lazy-loaders (check_*.sh) read real files from disk. writeFileSync remains
// mocked so we can capture generated scripts. Use vi.importActual to get the
// real readFileSync for the loaders while keeping the rest of the mock intact.
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

describe('writeCheckHelper — direct invocation', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('writes the Blueprint admin-ui/agent/backend blocks for the self-repo when no buildConfig is supplied', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-direct-1',
      workBranch: 'blueprint/check-direct-1',
      baseBranch: 'main',
      // Blueprint self-repo coordinates — required for the hardcoded blocks now.
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });

    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    expect(script).toMatch(/^#!\/bin\/bash/);
    expect(script).toContain('_VERIFY_TASK_ID="${TASK_ID:-check-direct-1}"');
    expect(script).toContain('_VERIFY_CAPABILITY_RECEIPT="/tmp/execution_capability_receipt_${_VERIFY_TASK_ID}"');
    expect(script).toContain('if ! rm -f "$_VERIFY_RECEIPT" "$_VERIFY_HARNESS_RECEIPT" "$_VERIFY_CAPABILITY_RECEIPT" \\\n  || ! rm -rf "$BLUEPRINT_GUARD_SUPPRESSION_DIR"; then');
    // Warm VMs reuse /tmp: a prior task's per-lane suppression receipts must be
    // cleared at startup, exactly as the verification receipts above are.
    expect(script).toContain('BLUEPRINT_GUARD_SUPPRESSION_DIR="/tmp/guard_suppression_receipts_${_VERIFY_TASK_ID}"');
    expect(script).toContain('export BLUEPRINT_GUARD_SUPPRESSION_DIR');
    // Every lane's suppressions are merged into one cross-lane aggregate. The
    // capture-skipped branch is the only viewport suppression still emitted from
    // bash: the suite itself runs through the attributed Vitest guard, which
    // records a warn-passed inherited viewport failure under its own
    // `vitest:admin-ui-viewport` lane instead of a second baseline compare here.
    expect(script).toContain('guard_suppression_receipt.py aggregate --out "$BLUEPRINT_GUARD_SUPPRESSION_DIR/aggregate.json"');
    expect(script).toContain('guard_suppression_receipt.py record --lane viewport --state capture_skipped');
    expect(script).toContain(
      'python3 scripts/vitest_verification_guard.py --suite admin-ui-viewport --base "$BASE" --repo-root "$WORKSPACE"',
    );
    // Blueprint blocks are the default on the self-repo when no buildConfig is supplied.
    expect(script).toContain('if admin_ui_runtime_touched; then');
    expect(script).toContain('touches agent/');
    expect(script).toContain('touches backend/');
    expect(script).toContain('admin-ui playwright chromium');
    // The admin-ui typecheck runs on the agent VM (safe, no OOM); vite bundle
    // is deferred to CI where it has room to breathe. The build half goes
    // through the shared base-attribution guard rather than a raw whole-project
    // compile, so an inherited type error isn't blamed on this task. That guard
    // also compiles the test-file project against the same base ref.
    expect(script).toContain(
      'run "admin-ui typecheck (attributed)" python3 scripts/typecheck_verification_guard.py --suite admin-ui --base "$BASE" --repo-root "$WORKSPACE"',
    );
    expect(script).not.toContain('run "admin-ui typecheck (test files)"');
    expect(script).not.toContain('npm run typecheck 2>&1'); // no unattributed compile
    expect(script).not.toContain('npm run build 2>&1'); // no vite build on agent VM
    // The agent suite remains always-on, but its result now comes from the
    // shared fail-closed base-attribution guard rather than console output.
    expect(script).toContain('run "agent vitest (attributed)" python3 scripts/vitest_verification_guard.py --suite agent --base "$BASE" --repo-root "$WORKSPACE"');
    // It lives inside the agent stack block, so it only runs when agent/ changed.
    expect(script).toMatch(/if touches agent\/; then[\s\S]*run "agent vitest \(attributed\)"/);
    // The admin-ui suite uses the same base-attributed lane and stays outside
    // the developer-only test switch in the generated publication gate.
    expect(script).toContain('run "admin-ui vitest (attributed)" python3 scripts/vitest_verification_guard.py --suite admin-ui --base "$BASE" --repo-root "$WORKSPACE"');
    expect(script).toMatch(/if admin_ui_runtime_touched; then[\s\S]*run "admin-ui vitest \(attributed\)"/);
    // The generated backend corpus lane delegates baseline attribution to the
    // shared static guard. This prevents a red base corpus from waiving every
    // current failure and keeps task ownership fail-closed.
    expect(script).toContain('run "backend pytest (attributed)" python3 scripts/static_release_guard.py');
    expect(script).toContain('--corpus-only --base "$BASE" --repo-root "$WORKSPACE"');
    expect(script).not.toContain('BPYTEST_BASELINE_FAILED');
    expect(script).not.toContain('backend pytest WARN — also broken on $BPYTEST_BASE_REF; not blocking PR');
    // Conflict-resolution verification shares this one attributed backend lane;
    // a second raw Postgres invocation would charge inherited failures twice.
    expect(script).not.toContain('merge-resolution closed-loop verify');
    expect(script).not.toContain('merge-resolution verify');
    expect(script).toContain('PASS — all checks green');
    // Size guardrail is wired into the self-repo gate: invoked by reference as
    // the committed checker script, run UNCONDITIONALLY across all sections
    // (not diff-scoped) — a rebase/merge can grow a file in a stack the diff
    // never touched, and the check is sub-second, so it's cheap always-on
    // insurance against burning a CI iteration on a common, trivial breach.
    expect(script).toContain('python3 backend/testing/check_size_limits.py > "$SIZE_GR_LOG" 2>&1');
    // Baseline-recheck: on failure, the checker re-runs with --base-root
    // against a detached worktree of the base branch so a breach INHERITED
    // from the base (e.g. an upstream merge pushed a chronic offender over
    // its cap on automain) warn-passes instead of blocking every unrelated
    // PR — mirroring the viewport/backend-pytest baseline pattern. A NEW or
    // GROWN breach still fails closed.
    expect(script).toContain('size guardrail FAILED — checking baseline on origin/$BASE');
    expect(script).toContain('python3 backend/testing/check_size_limits.py --base-root "$SIZE_GR_WT"');
    expect(script).toContain('size guardrail WARN — every offender pre-exists on $SIZE_GR_BASE_REF; not blocking this PR');
    expect(script).toContain('FAILED+=("size guardrail")');
    // --full skips the baseline recheck (the result IS the new baseline),
    // matching the viewport gate, so an agent can force a blocking view of a
    // baseline-warned WARN.
    expect(script).toContain('size guardrail FAILED (--full, no baseline recheck)');
    // It must NOT be gated on the touched stacks anymore.
    expect(script).not.toContain('SIZE_SECTIONS');
    expect(script).not.toContain('--sections $SIZE_SECTIONS');
    // npm install is guarded by a package-lock hash, not the bare existence of
    // node_modules — a warm VM whose lockfile changed must actually reinstall.
    expect(script).toContain('npm_ci_if_lock_changed() {');
    expect(script).toContain('( cd admin-ui && npm_ci_if_lock_changed )');
    expect(script).toContain('( cd agent && npm_ci_if_lock_changed )');
    // The stale `[ -d node_modules ] || npm ci` skip-if-present guard is gone
    // from both call sites. (Match the invoked command forms — the phrase also
    // appears verbatim in the helper's explanatory NOTE comment, by design.)
    expect(script).not.toContain('( cd admin-ui && [ -d node_modules ] || npm ci )');
    expect(script).not.toContain('( cd agent && [ -d node_modules ] || npm ci )');
  }, 15_000);

  it('generated ./check guidance publishes named inherited failures but blocks un-attributed failures', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-guidance-attribution',
      workBranch: 'blueprint/check-guidance-attribution',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });
    const generated = capturedScripts['/tmp/workspace/.check-task'];
    expect(generated).toContain('named, structured failures confirmed inherited on the');
    expect(generated).toContain('publish through ./pr open with their verification receipt');
    expect(generated).toContain('New or unattributed failures, execution/attribution failures');
    expect(generated).toContain('explicit\n# coverage failures stay non-zero and block ./pr open');
  });

  it('routes the admin-ui viewport lane through the attributed verification guard', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-viewport-attributed',
      workBranch: 'blueprint/check-viewport-attributed',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });

    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();

    // Literal pins on the guard invocation — both scoping modes. The scoped
    // path forwards the already-computed VIEWPORT_FILTER as the CURRENT-side
    // selector list so the guard can reject an incomplete scope itself.
    expect(script).toContain(
      'python3 scripts/vitest_verification_guard.py --suite admin-ui-viewport --base "$BASE" --repo-root "$WORKSPACE"'
    );
    expect(script).toContain(
      'python3 scripts/vitest_verification_guard.py --suite admin-ui-viewport --base "$BASE" --repo-root "$WORKSPACE" --current-files $VIEWPORT_FILTER'
    );
    // Unscoped guard call is reachable only from --full / non-component changes.
    expect(script).toContain('if [ "$FULL_SCOPE" = "1" ] || [ -n "$NON_COMPONENT" ]; then');
    // A non-zero guard exit blocks; there is no waiver branch left.
    expect(script).toContain('FAILED+=("$VIEWPORT_LABEL")');
    expect(script).toContain('[check] $VIEWPORT_LABEL FAILED — blocking per attributed viewport guard.');

    // The whole-run bash compare is DELETED, not left armed alongside the guard.
    expect(script).not.toContain('BASELINE_FAILED');
    expect(script).not.toContain('WARN — also broken on $BASE_REF; not blocking PR');
    expect(script).not.toContain('git worktree add --detach "$WT"');
    expect(script).not.toContain('/tmp/check_viewport_baseline_wt');
    expect(script).not.toContain('passes on $BASE_REF, fails on PR. Real regression.');
    expect(script).not.toContain('$VIEWPORT_LABEL FAILED (--full, no baseline recheck)');

    // The lane no longer shells out to `npm run test:viewport`, whose npm
    // script chained the entire unfiltered happy-dom unit suite ahead of the
    // browser run — on BOTH sides of the old compare. The capture-mode script
    // (a different npm script) is untouched.
    expect(script).not.toContain('npm run test:viewport -- $VIEWPORT_FILTER');
    expect(script).not.toContain('npm run test:viewport 2>&1');
    // The coverage invariants that npm script used to chain in are NOT
    // re-added as a standalone unattributed lane: they are asserted by
    // admin-ui/src/test/viewport-coverage.test.ts, which the unit config
    // includes, so the attributed `--suite admin-ui` lane already enforces
    // them with base attribution. An explicit step here would hard-block on
    // inherited violations the attributed lane correctly warn-passes.
    expect(script).not.toContain('check-viewport-coverage.js');
    expect(script).toContain(
      'run "admin-ui vitest (attributed)" python3 scripts/vitest_verification_guard.py --suite admin-ui'
    );

    // Preserved surrounding behavior that is NOT the defect.
    expect(script).toContain('no viewport test matches changed components, skipping viewport suite');
    expect(script).toContain('VIEWPORT_CAPTURE_SKIPPED: Chromium substrate unavailable on this VM');
    expect(script).toContain('admin-ui new component viewport test');
    expect(script).toContain('VIEWPORT_LABEL="admin-ui viewport (full, --full)"');
    // The screenshot-upload evidence block still fires on a blocking failure
    // and still reads the JSON manifest the guard keeps emitting.
    expect(script).toContain('admin-ui/test-results/viewport-failures.json');
    expect(script.indexOf('/api/agent/viewport/failure-screenshot-upload-url')).toBeGreaterThan(
      script.indexOf('[check] $VIEWPORT_LABEL FAILED — blocking per attributed viewport guard.')
    );
  }, 15_000);

  it('uses the repo-backed reverse import index for scoped viewport selection', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-viewport-reverse-index',
      workBranch: 'blueprint/check-viewport-reverse-index',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });

    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toContain(
      'python3 scripts/viewport_scope.py --repo-root "$WORKSPACE" --changed-files-file /tmp/check_viewport_changed.txt > /tmp/check_viewport_tests.txt',
    );
    expect(script).toContain('DIRECT relative imports from viewport tests to changed');
    expect(script).toContain('one level only');
    expect(script).toContain('if [ "$FULL_SCOPE" = "1" ] || [ -n "$NON_COMPONENT" ]; then');
    expect(script).toContain('--current-files $VIEWPORT_FILTER');
    expect(script).not.toContain('vt="${f%.tsx}.viewport.test.tsx"');
  });

  it('emits a generic no-op fallback (no Blueprint blocks) for a foreign repo with no buildConfig', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-direct-foreign',
      workBranch: 'blueprint/check-direct-foreign',
      baseBranch: 'main',
      owner: 'some-org',
      repo: 'some-other-repo',
      testsOnMainBranch: true,
    });

    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    expect(script).toMatch(/^#!\/bin\/bash/);
    // Generic fallback: no Blueprint-stack commands run against an unknown repo.
    expect(script).toContain('no build_config configured for some-org/some-other-repo');
    expect(script).not.toContain('if admin_ui_runtime_touched; then');
    expect(script).not.toContain('run "agent tsc');
    expect(script).not.toContain('admin-ui playwright chromium');
    // The Blueprint-specific size guardrail does not run against a foreign repo.
    expect(script).not.toContain('check_size_limits.py');
    // Still a valid check that reports PASS when nothing failed.
    expect(script).toContain('PASS — all checks green');
  });

  it('generated ./check script passes `bash -n` syntax check (Blueprint self-repo path)', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-direct-2',
      workBranch: 'blueprint/check-direct-2',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    const { execSync } = await import('child_process');
    // Pipe through stdin so we don't need a writable fs (vi.mock('fs') is in effect).
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  it('generic fallback ./check script passes `bash -n` syntax check', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-direct-foreign-2',
      workBranch: 'blueprint/check-direct-foreign-2',
      baseBranch: 'main',
      owner: 'some-org',
      repo: 'some-other-repo',
      testsOnMainBranch: true,
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  it('keeps the self-repo size guardrail on the config-driven stack path', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-direct-3',
      workBranch: 'blueprint/check-direct-3',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: false,
      buildConfig: {
        directories: [
          { path: 'svc', type: 'python', lint_cmd: 'ruff check .', build_cmd: 'python -m build' },
        ],
      },
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    // Config-driven path emits the dedicated comment and per-directory blocks
    // instead of the hardcoded Blueprint blocks.
    expect(script).toContain('Config-driven checks');
    expect(script).toContain(`if touches "svc/"; then`);
    expect(script).toContain(`run "svc build" bash -c 'cd "svc" && python -m build'`);
    // Recognized linter (ruff) gets a scoped FULL_SCOPE block.
    expect(script).toContain('if [ "$FULL_SCOPE" = "1" ]; then');
    // Hardcoded Blueprint blocks are NOT invoked on the config-driven path.
    // (The admin_ui_runtime_touched helper function definition is in the
    // shared header — what we check for is that it's not invoked here.)
    expect(script).not.toContain('if admin_ui_runtime_touched; then');
    expect(script).not.toContain('run "agent tsc');
    // The self-repo invariant stays armed on the config-driven path.
    expect(script).toContain('python3 backend/testing/check_size_limits.py > "$SIZE_GR_LOG" 2>&1');
    expect(script).toContain('size guardrail FAILED — checking baseline on origin/$BASE');
  });

  it('validateCfgShellSafety rejects unsafe taskId when called via writeCheckHelper', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    expect(() => {
      writeCheckHelper({
        taskId: 'bad;rm -rf /',
        workBranch: 'blueprint/bad',
        baseBranch: 'main',
        owner: 'test-owner',
        repo: 'test-repo',
        testsOnMainBranch: false,
      });
    }).toThrow(/taskId contains unsafe characters/);
  });

  it('writes to .check-task for the Blueprint self-repo and to check for foreign repos', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');

    // Self-repo: must land in .check-task (gitignored), not in the tracked check file.
    writeCheckHelper({
      taskId: 'outfile-self',
      workBranch: 'blueprint/outfile-self',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });
    expect(capturedScripts['/tmp/workspace/.check-task']).toBeDefined();
    expect(capturedScripts['/tmp/workspace/check']).toBeUndefined();

    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });

    // Foreign repo: must land in check, not in .check-task.
    writeCheckHelper({
      taskId: 'outfile-foreign',
      workBranch: 'blueprint/outfile-foreign',
      baseBranch: 'main',
      owner: 'some-org',
      repo: 'some-other-repo',
      testsOnMainBranch: true,
    });
    expect(capturedScripts['/tmp/workspace/check']).toBeDefined();
    expect(capturedScripts['/tmp/workspace/.check-task']).toBeUndefined();
  });
});

// ── Visual acceptance evidence success-path capture+upload ───────────────────
// GAP 3 of the visual-acceptance loop: nothing uploaded a screenshot on a
// SUCCESSFUL run, so a declared `visual` acceptance evidence could never
// resolve. writeCheckHelper now emits a best-effort success-path capture+upload
// block, presence-gated on cfg.visualAcceptance. These tests pin (a) the block
// is emitted ONLY when a visual declaration is configured, (b) it is otherwise
// byte-identical to today, and (c) it captures+uploads EXACTLY the declared
// (component, width) so the canonical context URI resolves.
describe('writeCheckHelper — visual acceptance capture+upload (success path)', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const baseCfg = {
    workBranch: 'blueprint/visual-1',
    baseBranch: 'automain',
    owner: 'HenryHamster',
    repo: 'BlueprintAIStudio',
    testsOnMainBranch: true,
  };

  it('emits the success-path viewport screenshot capture+upload block only when a visual acceptance declaration is configured', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');

    // WITHOUT a visual declaration: no capture+upload block at all.
    writeCheckHelper({ ...baseCfg, taskId: 'visual-off' });
    const without = capturedScripts['/tmp/workspace/.check-task'];
    expect(without).toBeDefined();
    expect(without).not.toContain('visual acceptance evidence capture+upload');
    expect(without).not.toContain('test:viewport:capture');

    // WITH a visual declaration: the block appears and drives the declared pair.
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
    writeCheckHelper({
      ...baseCfg,
      taskId: 'visual-on',
      visualAcceptance: { component: 'MetricsDashboard', width: 375 },
    });
    const withVisual = capturedScripts['/tmp/workspace/.check-task'];
    expect(withVisual).toBeDefined();
    // It is a SUCCESS-path block: gated on no failed checks + admin-ui touched.
    expect(withVisual).toContain('visual acceptance evidence capture+upload (success path)');
    expect(withVisual).toContain('if [ ${#FAILED[@]} -eq 0 ] && admin_ui_runtime_touched');
    // Captures via the capture-mode config, scoped to the declared component.
    expect(withVisual).toContain('npm run test:viewport:capture -- "$_VA_COMPONENT"');
    expect(withVisual).toContain(`_VA_COMPONENT='MetricsDashboard'`);
    expect(withVisual).toContain('_VA_WIDTH=375');
    // Finds the PNG for EXACTLY the declared width.
    expect(withVisual).toContain('-name "*-${_VA_WIDTH}px.png"');
    // POSTs {task_id, component, width} to the signed-URL endpoint and PUTs the PNG.
    expect(withVisual).toContain('/api/agent/viewport/failure-screenshot-upload-url');
    expect(withVisual).toContain('\\"task_id\\":\\"visual-on\\"');
    expect(withVisual).toContain('-T "$_VA_PNG" -H "Content-Type: image/png"');
  });

  it('the generated ./check is byte-identical outside the visual block (block-removal round-trips)', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({ ...baseCfg, taskId: 'visual-id' });
    const without = capturedScripts['/tmp/workspace/.check-task'];

    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
    writeCheckHelper({
      ...baseCfg,
      taskId: 'visual-id',
      visualAcceptance: { component: 'TaskCard', width: 768 },
    });
    const withVisual = capturedScripts['/tmp/workspace/.check-task'];

    // Splice the visual block back out — the remainder must be byte-for-byte
    // identical to the no-declaration script (presence-gated, additive only).
    const start = withVisual.indexOf('\n# ── visual acceptance evidence capture+upload (success path)');
    const endText = '# ── end visual acceptance evidence capture+upload';
    const endIdx = withVisual.indexOf(endText);
    const endOfLine = withVisual.indexOf('\n', endIdx) + 1;
    expect(start).toBeGreaterThan(-1);
    expect(endIdx).toBeGreaterThan(-1);
    const reconstructed = withVisual.slice(0, start) + withVisual.slice(endOfLine);
    expect(reconstructed).toBe(without);
  });

  it('the generated ./check with a visual declaration passes `bash -n`', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      ...baseCfg,
      taskId: 'visual-syntax',
      visualAcceptance: { component: 'OrchestratorHealthBar', width: 1280 },
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  it('validateCfgShellSafety rejects an unsafe visual component name', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    expect(() => {
      writeCheckHelper({
        ...baseCfg,
        taskId: 'visual-unsafe',
        visualAcceptance: { component: "Foo'; rm -rf /", width: 375 },
      });
    }).toThrow(/visualAcceptance\.component contains unsafe characters/);
  });

  it('ignores a visual declaration on a foreign repo (config-less, non-self-repo)', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'visual-foreign',
      workBranch: 'blueprint/visual-foreign',
      baseBranch: 'main',
      owner: 'some-org',
      repo: 'some-other-repo',
      testsOnMainBranch: true,
      visualAcceptance: { component: 'MetricsDashboard', width: 375 },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    // No Blueprint admin-ui viewport harness on a foreign repo → no capture block.
    expect(script).not.toContain('visual acceptance evidence capture+upload');
  });

  it('emits capture block for visual declaration + config-driven stack with admin-ui', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'visual-config-driven',
      workBranch: 'blueprint/visual-config-driven',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
      visualAcceptance: { component: 'TaskCard', width: 768 },
      buildConfig: {
        directories: [
          { path: 'admin-ui', type: 'node', build_cmd: 'npm run build' },
          { path: 'backend', type: 'python', build_cmd: 'python -m build' },
        ],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    // Config-driven stack with admin-ui configured → should capture, not skip.
    expect(script).toContain('visual acceptance evidence capture+upload (success path)');
    expect(script).toContain("_VA_COMPONENT='TaskCard'");
    expect(script).toContain('_VA_WIDTH=768');
    expect(script).toContain('npm run test:viewport:capture');
    // NOT a skip block.
    expect(script).not.toContain('visual acceptance declared but');
  });

  it('emits loud skip for visual declaration + config-driven stack WITHOUT admin-ui', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'visual-config-no-admin-ui',
      workBranch: 'blueprint/visual-config-no-admin-ui',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
      visualAcceptance: { component: 'SomeComponent', width: 1024 },
      buildConfig: {
        directories: [
          { path: 'backend', type: 'python', build_cmd: 'python -m build' },
          { path: 'frontend', type: 'node', build_cmd: 'npm run build' },
        ],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    // Config-driven stack WITHOUT admin-ui → should emit loud skip, not capture.
    expect(script).toContain('visual acceptance declared but admin-ui not configured in build_config');
    // NOT a capture block.
    expect(script).not.toContain('visual acceptance evidence capture+upload (success path)');
    expect(script).not.toContain('npm run test:viewport:capture');
  });

  it('loud skip message for undeclared visual passes `bash -n` syntax check', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'visual-skip-syntax',
      workBranch: 'blueprint/visual-skip-syntax',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
      visualAcceptance: { component: 'Panel', width: 768 },
      buildConfig: {
        directories: [
          { path: 'backend', type: 'python' },
        ],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  it('byte-identical ./check when visual acceptance is not declared (no visual block at all)', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    // Config-driven WITHOUT visual declaration
    writeCheckHelper({
      taskId: 'no-visual-id',
      workBranch: 'blueprint/no-visual-id',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
      buildConfig: {
        directories: [
          { path: 'admin-ui', type: 'node', build_cmd: 'npm run build' },
        ],
      },
    });
    const configDrivenNoVisual = capturedScripts['/tmp/workspace/check'];

    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
    // Same config WITH visual declaration
    writeCheckHelper({
      taskId: 'no-visual-id',
      workBranch: 'blueprint/no-visual-id',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
      visualAcceptance: { component: 'X', width: 375 },
      buildConfig: {
        directories: [
          { path: 'admin-ui', type: 'node', build_cmd: 'npm run build' },
        ],
      },
    });
    const configDrivenWithVisual = capturedScripts['/tmp/workspace/check'];

    // Splice out the capture block — remainder must be byte-identical.
    const start = configDrivenWithVisual.indexOf('\n# ── visual acceptance evidence');
    const endText = '# ── end visual acceptance evidence';
    const endIdx = configDrivenWithVisual.indexOf(endText);
    const endOfLine = configDrivenWithVisual.indexOf('\n', endIdx) + 1;
    expect(start).toBeGreaterThan(-1);
    expect(endIdx).toBeGreaterThan(-1);
    const reconstructed = configDrivenWithVisual.slice(0, start) + configDrivenWithVisual.slice(endOfLine);
    expect(reconstructed).toBe(configDrivenNoVisual);
  });
});

// ── Config-driven test commands ──────────────────────────────────────────────
// The generic pytest decision ladder was retired in favor of the retained
// project-verification attributor. Ordinary configured commands still use the
// shared run wrapper, so a genuinely failing test remains blocking.
describe('writeCheckHelper — configured test commands', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const render = async (
    directory: DirectoryConfig,
    taskId = 'configured-test-1',
  ): Promise<string> => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'other-repo',
      testsOnMainBranch: true,
      taskCapability: { agent_mode: 'write', run_shape: 'workflow', output_kind: 'pr' },
      buildConfig: { directories: [directory] },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    return script;
  };

  it('keeps an ordinary pytest command on the shared fail-closed run path', async () => {
    const script = await render({ path: 'backend', type: 'python', test_cmd: 'pytest' });
    expect(script).toContain(`run "backend test" bash -c 'cd "backend" && pytest'`);
    expect(script).not.toContain('_GATE_MODE');
    expect(script).not.toContain('GATE-ENVIRONMENT:');
    expect(script).not.toContain('test (sanctioned harness)');
  });

  it('routes an explicit project-verification target to the retained attributor', async () => {
    const script = await render({
      path: 'backend',
      type: 'python',
      lint_cmd: 'ruff check .',
      test_cmd: 'pytest',
      project_verification: {
        protocol: 'blueprint-v1',
        harness: 'scripts/test-backend.sh',
      },
    }, 'configured-verification-1');
    // The retained block is capability-gated; ordinary external config does
    // not accidentally mint the receipt contract.
    expect(script).toContain('required backend-authoring verification');
    expect(script).toContain('backend-test-route.sh');
    expect(script).not.toContain(`run "backend test" bash -c 'cd "backend" && pytest'`);
  });

  it('preserves generated bash syntax and a genuinely failing configured test', async () => {
    const script = await render({ path: 'service', type: 'python', test_cmd: 'pytest -q' }, 'configured-failure-1');
    expect(script).toContain(`run "service test" bash -c 'cd "service" && pytest -q'`);
    expect(script).toContain('FAILED+=("$label")');
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });
});


describe('writeCheckHelper — required backend-authoring verification', () => {
  let capturedScripts: Record<string, string> = {};
  const authoringCapability = { agent_mode: 'write', run_shape: 'workflow', output_kind: 'pr' };
  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });
  const render = async (taskCapability: Record<string, unknown> = authoringCapability): Promise<string> => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'verify-authoring-1',
      workBranch: 'blueprint/verify-authoring-1',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: false,
      taskCapability,
      buildConfig: {
        directories: [{
          path: 'backend',
          type: 'python',
          lint_cmd: 'ruff check .',
          test_cmd: 'python3 -m pytest',
          project_verification: {
            protocol: 'blueprint-v1',
            harness: 'scripts/test-backend.sh',
          },
        }],
      },
    });
    return capturedScripts['/tmp/workspace/.check-task'];
  };
  it('derives the requirement from capability axes and configured Ruff+pytest', async () => {
    const script = await render();
    expect(script).toContain('required backend-authoring verification');
    expect(script).toContain('python3 -m ruff check --select E,F,W --ignore E501');
    expect(script).toContain('USE_TEST_POSTGRES=true bash "$_VERIFICATION_HARNESS" "$_verification_postgres_flag"');
    expect(script).toContain('source scripts/backend-test-route.sh');
    expect(script).toContain('select_backend_test_route');
    expect(script).toContain(
      '_VERIFY_RECEIPT="/tmp/project_verification_receipt_${_VERIFY_TASK_ID}.json"',
    );
    expect(script).toContain('--current-exit-code "$_verification_exit"');
    // The four structured attribution maps reach the sidecar through the real,
    // directly tested writer when the workspace ships it. The inline python3 -c
    // heredoc survives only as the guarded fallback for a foreign workspace
    // that has no scripts/verification_receipt.py (see the foreign-repo test
    // below) — it must never be reached when the writer IS present.
    expect(script).toContain('if [ -r scripts/verification_receipt.py ]; then');
    expect(script).toContain(
      'python3 scripts/verification_receipt.py backend-pytest --receipt "$_VERIFY_RECEIPT"',
    );
    // Read from a FILE, never argv: one traceback per suppressed baseline
    // failure would otherwise blow the OS argv-size limit (E2BIG).
    expect(script).toContain('--attribution-file "$_verification_attribution_file"');
    expect(script).toContain('elif python3 -c ');
    expect(script).toContain('"suppressed_failures":d.get("suppressed_failures",{})');
    expect(script).toContain('BLUEPRINT_VERIFICATION_RECEIPT="$_VERIFY_RECEIPT"');
    expect(script).toContain('export BLUEPRINT_VERIFICATION_RECEIPT');
    expect(script).toContain('no impacted pytest file/node was resolved');
    expect(script).toContain("grep -E '^backend/(testing|tests)/test_.*\\.py$'");
    expect(script).toContain('PostgreSQL runtime unavailable');
    expect(script).toContain('sanctioned harness missing');
    expect(script).not.toContain('touches "backend/" && [ "$WITH_TESTS" = "1" ]');
  });
  // The fragment is emitted for ANY project whose build_config declares the
  // blueprint-v1 verification protocol -- not only the Blueprint self-repo. A
  // foreign workspace has no scripts/verification_receipt.py, so an unguarded
  // dependency on it would make python3 exit non-zero, push
  // `FAILED+=("... verification receipt")`, and block every ./check (and
  // therefore every ./pr open) in that project even when the targeted Postgres
  // pytest lane passed.
  it('keeps a self-contained receipt writer for a foreign repo that lacks the Blueprint helper', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'verify-foreign-1',
      workBranch: 'blueprint/verify-foreign-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'not-blueprint',
      testsOnMainBranch: false,
      taskCapability: authoringCapability,
      buildConfig: {
        directories: [{
          path: 'service',
          type: 'python',
          lint_cmd: 'ruff check .',
          test_cmd: 'python3 -m pytest',
          project_verification: {
            protocol: 'blueprint-v1',
            harness: 'tools/verified-postgres-tests.sh',
          },
        }],
      },
    });
    // A foreign repo gets the gate written straight to `check`; the
    // `.check-task` delegation wrapper is a self-repo-only arrangement, which
    // is itself part of why this path had no coverage.
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toContain('required backend-authoring verification');
    // Every reference to the Blueprint-local writer is behind the readability
    // guard, and the fallback that runs without it writes the same receipt.
    const guardAt = script.indexOf('if [ -r scripts/verification_receipt.py ]; then');
    const fallbackAt = script.indexOf('elif python3 -c ');
    const verdictAt = script.indexOf('if [ "$_verification_receipt_written" = "1" ]; then');
    expect(guardAt).toBeGreaterThan(-1);
    expect(fallbackAt).toBeGreaterThan(guardAt);
    expect(verdictAt).toBeGreaterThan(fallbackAt);
    // Structural: every invocation of the Blueprint-local helper sits INSIDE
    // the readability guard, so its absence takes the `elif` fallback rather
    // than exiting non-zero.
    for (
      let at = script.indexOf('python3 scripts/verification_receipt.py backend-pytest');
      at !== -1;
      at = script.indexOf('python3 scripts/verification_receipt.py backend-pytest', at + 1)
    ) {
      expect(at).toBeGreaterThan(guardAt);
      expect(at).toBeLessThan(fallbackAt);
    }
    // The self-contained fallback writes the same four attribution maps.
    expect(script).toContain('"suppressed_failures":d.get("suppressed_failures",{})');
    expect(script).toContain('FAILED+=("$_VERIFICATION_DIR verification receipt")');
  });
  it('does not apply the gate to canonical read-only or compute capabilities', async () => {
    for (const capability of [
      { agent_mode: 'read_only', run_shape: 'single_agent', output_kind: 'knowledge' },
      { agent_mode: 'write', run_shape: 'experiment_loop', output_kind: 'artifact' },
    ]) {
      const script = await render(capability);
      expect(script).not.toContain('required backend-authoring verification');
      expect(script).not.toContain('project_verification_receipt_verify-authoring-1.json');
    }
  });
  it('does not infer a requirement when either configured command is absent', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'verify-authoring-no-pytest',
      workBranch: 'blueprint/verify-authoring-no-pytest',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'python-only',
      testsOnMainBranch: true,
      taskCapability: authoringCapability,
      buildConfig: {
        directories: [{ path: 'service', type: 'python', lint_cmd: 'ruff check .' }],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).not.toContain('required backend-authoring verification');
    expect(script).not.toContain('project_verification_receipt_verify-authoring-no-pytest.json');
  });
  it.each(['.', 'services/api', 'service'])(
    'does not impose the Blueprint receipt contract on ordinary Ruff+pytest project path %s',
    async projectPath => {
      const { writeCheckHelper } = await import('./check-helper.js');
      writeCheckHelper({
        taskId: 'verify-external-project',
        workBranch: 'blueprint/verify-external-project',
        baseBranch: 'main',
        owner: 'acme',
        repo: 'python-service',
        testsOnMainBranch: true,
        taskCapability: authoringCapability,
        buildConfig: {
          directories: [{
            path: projectPath,
            type: 'python',
            lint_cmd: 'ruff check .',
            test_cmd: 'pytest',
          }],
        },
      });
      const script = capturedScripts['/tmp/workspace/check'];
      expect(script).not.toContain('required backend-authoring verification');
      expect(script).not.toContain('BLUEPRINT_VERIFICATION_RESULT_FILE');
    },
  );
  it('an explicitly opted-in root project matches repo-relative Python changes', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'verify-root-project',
      workBranch: 'blueprint/verify-root-project',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'python-root',
      testsOnMainBranch: true,
      taskCapability: authoringCapability,
      buildConfig: {
        directories: [{
          path: '.',
          type: 'python',
          lint_cmd: 'ruff check .',
          test_cmd: 'pytest',
          project_verification: {
            protocol: 'blueprint-v1',
            harness: 'tools/verified-postgres-tests.sh',
          },
        }],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toContain(`case "$f" in *.py) [ -f "$f" ] && printf x && break`);
    expect(script).toContain("grep -E '.*\\.py$'");
    expect(script).toContain("grep -E '^(testing|tests)/test_.*\\.py$'");
    expect(script).toContain("_VERIFICATION_HARNESS='tools/verified-postgres-tests.sh'");
    const requiredBlock = script.slice(script.indexOf('required backend-authoring verification'));
    expect(requiredBlock).not.toContain('if touches "./"');
  });
  it('canonicalizes opted-in directory paths and rejects noncanonical harnesses', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    const baseConfig = {
      taskId: 'verify-canonical-project',
      workBranch: 'blueprint/verify-canonical-project',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'python-service',
      testsOnMainBranch: true,
      taskCapability: authoringCapability,
    };
    writeCheckHelper({
      ...baseConfig,
      buildConfig: { directories: [{
        path: ' backend/// ', type: 'python',
        lint_cmd: 'ruff check .', test_cmd: 'pytest',
        project_verification: {
          protocol: 'blueprint-v1', harness: 'scripts/test-backend.sh',
        },
      }] },
    });
    expect(capturedScripts['/tmp/workspace/check']).toContain(
      'case "$f" in backend/*.py) [ -f "$f" ] && printf x && break',
    );

    writeCheckHelper({
      ...baseConfig,
      buildConfig: { directories: [{
        path: './backend', type: 'python',
        lint_cmd: 'ruff check .', test_cmd: 'pytest',
        project_verification: {
          protocol: 'blueprint-v1', harness: 'scripts/test-backend.sh',
        },
      }] },
    });
    const dotPrefixedScript = capturedScripts['/tmp/workspace/check'];
    expect(dotPrefixedScript).toContain(
      'case "$f" in backend/*.py) [ -f "$f" ] && printf x && break',
    );
    expect(dotPrefixedScript).not.toContain('case "$f" in ./backend/*.py');

    expect(() => {
      writeCheckHelper({
        ...baseConfig,
        buildConfig: { directories: [{
          path: 'backend', type: 'python',
          lint_cmd: 'ruff check .', test_cmd: 'pytest',
          project_verification: {
            protocol: 'blueprint-v1', harness: 'scripts/test backend.sh',
          },
        }] },
      });
    }).toThrow('project verification harness contains unsafe characters');
  });
  it('generated required-verification check passes bash syntax validation', async () => {
    const script = await render();
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });
  const runRequiredCheck = async (
    renderedScript: string,
    scenario: {
      ruffExit: number; docker: boolean; harnessExit: number; skipped?: number;
      changedPath?: string; changedPaths?: string[]; missingPaths?: string[];
      harnessPath?: string;
      /** Whether the baked sandbox interpreter exists (defaults to `docker`). */
      sandboxPython?: boolean;
      /** Whether a usable PostgreSQL server bindir exists (defaults to `docker`). */
      pgBindir?: 'present' | 'missing';
      routeHelper?: 'valid' | 'missing' | 'syntax-invalid';
      /** Seed a valid prior negative receipt; this run must clear or replace it. */
      seedCapabilityReceipt?: boolean;
    },
  ): Promise<{
    status: number;
    stdout: string;
    log: string;
    taskId: string;
    receipt?: Record<string, unknown>;
    capabilityReceipt?: string;
  }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'required-verification-'));
    const posix = (p: string): string =>
      p.replace(/\\/g, '/').replace(/^([A-Za-z]):\//, (_m, d: string) => `/${d.toLowerCase()}/`);
    // A process ID is not unique across reboots. Persistent self-hosted runners
    // can therefore inherit a same-named /tmp receipt owned by another runner
    // user, which the sticky directory correctly prevents this fixture from
    // deleting. Reuse mkdtemp's random suffix so every invocation has a
    // collision-proof task ID. Static assertions above still cover the
    // production task-ID contract byte-for-byte.
    const runtimeNonce = realPath.basename(dir).replace(/[^A-Za-z0-9_-]/g, '-');
    const runtimeTaskId = `verify-authoring-1-${process.pid}-${runtimeNonce}`;
    const runtimeTmp = realPath.join(dir, 'tmp');
    realFs.mkdirSync(runtimeTmp, { recursive: true });
    // The production helper intentionally uses host /tmp. Redirect every
    // runtime-fixture artifact into this invocation's private directory:
    // Vitest workers and persistent runner users must never contend for the
    // helper's fixed lint lists, logs, worktrees, or receipts. Static render
    // assertions continue to verify the real /tmp paths above.
    const script = renderedScript
      .replaceAll('verify-authoring-1', runtimeTaskId)
      .replaceAll('/tmp/', `${posix(runtimeTmp)}/`);
    const receiptPath = realPath.join(
      runtimeTmp,
      `project_verification_receipt_${runtimeTaskId}.json`,
    );
    const capabilityReceiptPath = realPath.join(
      runtimeTmp,
      `execution_capability_receipt_${runtimeTaskId}`,
    );
    try {
      try { realFs.unlinkSync(receiptPath); } catch { /* absent */ }
      if (scenario.seedCapabilityReceipt) {
        realFs.writeFileSync(
          capabilityReceiptPath,
          `backend_verification:runtime_unavailable:v1:${runtimeTaskId}\n`,
        );
      }
      const bin = realPath.join(dir, 'bin');
      const changedPaths = scenario.changedPaths ?? [scenario.changedPath ?? 'backend/x.py'];
      const missingPaths = new Set(scenario.missingPaths ?? []);
      const harnessPath = scenario.harnessPath ?? 'scripts/test-backend.sh';
      // The gate now requires BOTH a usable PostgreSQL server bindir AND the
      // baked sandbox interpreter before selecting --sandbox-postgres. The
      // runtime mock cannot write /opt, so model the baked venv inside the
      // sandbox dir and pin BLUEPRINT_SANDBOX_PYTHON to it in the exec env.
      const pgBindirPresent = scenario.pgBindir ?? scenario.docker;
      const sandboxPythonPresent = scenario.sandboxPython ?? scenario.docker;
      const sandboxPythonDir = realPath.join(dir, 'sandbox-venv', 'bin');
      const sandboxPythonPath = realPath.join(sandboxPythonDir, 'python');
      if (sandboxPythonPresent) {
        realFs.mkdirSync(sandboxPythonDir, { recursive: true });
        realFs.writeFileSync(sandboxPythonPath, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      }
      realFs.mkdirSync(bin, { recursive: true });
      realFs.mkdirSync(realPath.join(dir, 'backend', 'testing'), { recursive: true });
      realFs.mkdirSync(realPath.join(dir, 'scripts'), { recursive: true });
      // The generated checker sources this committed helper at runtime. Copy
      // it into the isolated fixture so these tests exercise the same route
      // selection contract as a real checkout.
      const routeHelper = realPath.resolve(process.cwd(), '..', 'scripts', 'backend-test-route.sh');
      const fixtureRouteHelper = realPath.join(dir, 'scripts', 'backend-test-route.sh');
      if (scenario.routeHelper === 'syntax-invalid') realFs.writeFileSync(fixtureRouteHelper, 'if then\n');
      else if (scenario.routeHelper !== 'missing') realFs.copyFileSync(routeHelper, fixtureRouteHelper);
      const productionRoot = realPath.resolve(process.cwd(), '..');
      realFs.copyFileSync(
        realPath.join(productionRoot, 'scripts', 'backend_verification_guard.py'),
        realPath.join(dir, 'scripts', 'backend_verification_guard.py'),
      );
      // The backend lane merges its block into the shared multi-lane sidecar
      // through this helper instead of an inline heredoc, so the fixture needs
      // the real writer to reach a minted receipt.
      realFs.copyFileSync(
        realPath.join(productionRoot, 'scripts', 'verification_receipt.py'),
        realPath.join(dir, 'scripts', 'verification_receipt.py'),
      );
      // The guard records its suppressions through this sibling emitter; copy
      // it so the fixture exercises the same cross-lane receipt path as a real
      // checkout rather than the guard's missing-emitter fallback.
      realFs.copyFileSync(
        realPath.join(productionRoot, 'scripts', 'guard_suppression_receipt.py'),
        realPath.join(dir, 'scripts', 'guard_suppression_receipt.py'),
      );
      realFs.copyFileSync(
        realPath.join(productionRoot, 'scripts', 'static_release_guard.py'),
        realPath.join(dir, 'scripts', 'static_release_guard.py'),
      );
      realFs.copyFileSync(
        realPath.join(productionRoot, 'backend', 'testing', 'fixgate_attribution.py'),
        realPath.join(dir, 'backend', 'testing', 'fixgate_attribution.py'),
      );
      realFs.writeFileSync(realPath.join(dir, 'backend', 'x.py'), 'value = 1\n');
      realFs.writeFileSync(realPath.join(dir, 'backend', 'testing', 'test_x.py'), 'def test_x(): assert True\n');
      realFs.writeFileSync(realPath.join(dir, 'backend', 'testing', 'impacted_tests.py'), '# shimmed by python3\n');
      for (const changedPath of changedPaths) {
        if (missingPaths.has(changedPath)) continue;
        const changedFile = realPath.join(dir, changedPath);
        realFs.mkdirSync(realPath.dirname(changedFile), { recursive: true });
        realFs.writeFileSync(changedFile, changedPath.includes('test_') ? 'def test_x(): assert True\n' : 'value = 1\n');
      }
      const changedOutput = changedPaths.map(p => `'${p}'`).join(' ');
      realFs.writeFileSync(realPath.join(bin, 'git'), `#!/bin/sh
case "$*" in
  *"rev-parse --git-dir"*) echo .git; exit 0 ;;
  *"rev-parse --verify origin/automain"*) exit 0 ;;
  *"diff --name-only origin/automain...HEAD"*) printf '%s\\n' ${changedOutput}; exit 0 ;;
  *" archive "*) tar -cf - --exclude=.git .; exit $? ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      const pythonName = process.platform === 'win32' ? 'python' : 'python3';
      const realPython = realCp.execFileSync(
        'sh', ['-c', 'command -v "$1"', 'python-probe', pythonName], { encoding: 'utf8' },
      ).trim();
      realFs.writeFileSync(realPath.join(bin, 'python3'), `#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "ruff" ]; then
  echo "ruff $*" >> "$LOGFILE"
  exit ${scenario.ruffExit}
fi
if [ "$1" = "backend/testing/impacted_tests.py" ]; then
  # Match the real resolver's stdin contract. Exiting before the producer has
  # drained is racy under pipefail and can make printf report SIGPIPE (141).
  changed=$(cat)
  case "$changed" in *backend/testing/test_bar.py*) echo backend/testing/test_bar.py ;; esac
  echo backend/testing/test_x.py
  exit 0
fi
case "$*" in *backend/testing/check_size_limits.py*) exit 0 ;; esac
exec "${realPython}" "$@"
`, { mode: 0o755 });
      realFs.writeFileSync(realPath.join(bin, 'npm'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(
        realPath.join(bin, 'pg_config'),
        `#!/bin/sh\n[ "$1" = "--bindir" ] && echo "${pgBindirPresent ? posix(bin) : '/missing/postgres/bin'}"\n`,
        { mode: 0o755 },
      );
      realFs.writeFileSync(realPath.join(bin, 'initdb'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(
        realPath.join(bin, 'docker'),
        `#!/bin/sh\nexit ${scenario.docker ? 0 : 1}\n`,
        { mode: 0o755 },
      );
      const harnessFile = realPath.join(dir, harnessPath);
      realFs.mkdirSync(realPath.dirname(harnessFile), { recursive: true });
      realFs.writeFileSync(harnessFile, `#!/bin/sh
echo "harness USE_TEST_POSTGRES=\${USE_TEST_POSTGRES:-} $*" >> "$LOGFILE"
case "$*" in *--standalone*) exit 0 ;; esac
if [ -n "\${BLUEPRINT_VERIFICATION_JUNIT:-}" ] && [ ${scenario.harnessExit} -eq 0 ]; then
  mkdir -p "$(dirname -- "$BLUEPRINT_VERIFICATION_JUNIT")"
  if [ ${scenario.skipped ?? 0} -gt 0 ]; then
    printf '%s\\n' '<testsuite tests="1" failures="0" skipped="1"><testcase classname="testing.test_x" name="test_x"><skipped /></testcase></testsuite>' > "$BLUEPRINT_VERIFICATION_JUNIT"
  else
    printf '%s\\n' '<testsuite tests="1" failures="0"><testcase classname="testing.test_x" name="test_x" /></testsuite>' > "$BLUEPRINT_VERIFICATION_JUNIT"
  fi
fi
if [ -n "\${BLUEPRINT_VERIFICATION_RESULT_FILE:-}" ] && [ ${scenario.harnessExit} -eq 0 ]; then
  printf '%s\\n' '{"version":1,"targeted":true,"use_test_postgres":true,"postgres_sidecar":true,"tests":1,"skipped":${scenario.skipped ?? 0},"failures":0,"exit_code":0,"selectors":["testing/test_x.py"]}' > "$BLUEPRINT_VERIFICATION_RESULT_FILE"
fi
exit ${scenario.harnessExit}
`, { mode: 0o755 });
      const checkPath = realPath.join(dir, 'check');
      const logPath = realPath.join(dir, 'run.log');
      realFs.writeFileSync(checkPath, script, { mode: 0o755 });
      realFs.writeFileSync(logPath, '');
      let status = 0;
      let stdout = '';
      try {
        stdout = realCp.execFileSync('bash', [posix(checkPath)], {
          cwd: dir,
          env: {
            PATH: `${posix(bin)}:${process.env.PATH ?? ''}`,
            WORKSPACE: posix(dir),
            LOGFILE: posix(logPath),
            HOME: process.env.HOME ?? '',
            // Pin the baked-sandbox-interpreter path to a deterministic
            // location inside this sandbox (the runtime mock cannot write /opt):
            // when sandboxPython is present the shim exists so the
            // --sandbox-postgres branch is reachable; when absent the env var
            // points at a missing file so the gate deterministically falls
            // through to --postgres / fail-closed regardless of whether this
            // host happens to have /opt/blueprint-backend-venv installed.
            BLUEPRINT_SANDBOX_PYTHON: posix(sandboxPythonPath),
          },
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'pipe'],
        });
      } catch (err: unknown) {
        const e = err as { status?: number; stdout?: string; stderr?: string };
        status = e.status ?? -1;
        stdout = (e.stdout ?? '') + (e.stderr ?? '');
      }
      const receipt = realFs.existsSync(receiptPath)
        ? JSON.parse(realFs.readFileSync(receiptPath, 'utf8')) as Record<string, unknown>
        : undefined;
      const capabilityReceipt = realFs.existsSync(capabilityReceiptPath)
        ? realFs.readFileSync(capabilityReceiptPath, 'utf8')
        : undefined;
      return {
        status,
        stdout,
        log: realFs.readFileSync(logPath, 'utf8'),
        taskId: runtimeTaskId,
        receipt,
        capabilityReceipt,
      };
    } finally {
      try { realFs.unlinkSync(receiptPath); } catch { /* absent */ }
      try { realFs.unlinkSync(capabilityReceiptPath); } catch { /* absent */ }
      realFs.rmSync(dir, { recursive: true, force: true });
    }
  };
  it('runtime: mints a receipt only after real-shaped Ruff and targeted Postgres evidence', async () => {
    const result = await runRequiredCheck(await render(), {
      ruffExit: 0, docker: true, harnessExit: 0, seedCapabilityReceipt: true,
    });
    expect(result.status, result.stdout).toBe(0);
    expect(result.log).toContain('ruff -m ruff check');
    expect(result.log).toContain('harness USE_TEST_POSTGRES=true --sandbox-postgres testing/test_x.py');
    expect(result.receipt).toMatchObject({
      task_id: result.taskId,
      ruff: { exit_code: 0 },
      pytest: {
        selectors: ['testing/test_x.py'],
        targeted: true,
        use_test_postgres: true,
        postgres_sidecar: true,
        skipped: 0,
        exit_code: 0,
      },
    });
    expect(result.capabilityReceipt).toBeUndefined();
  });
  it('runtime: combines changed tests with resolver output, filters deletions, and deduplicates', async () => {
    const result = await runRequiredCheck(await render(), {
      ruffExit: 0,
      docker: true,
      harnessExit: 0,
      changedPaths: [
        'backend/x.py',
        'backend/testing/test_bar.py',
        'backend/testing/test_deleted.py',
        'backend/testing/test_x.py',
      ],
      missingPaths: ['backend/testing/test_deleted.py'],
    });
    expect(result.status, result.stdout).toBe(0);
    expect(result.log).toContain(
      'harness USE_TEST_POSTGRES=true --sandbox-postgres testing/test_bar.py testing/test_x.py',
    );
    expect(result.log).not.toContain('test_deleted.py');
    expect(result.receipt).toMatchObject({
      pytest: { selectors: ['testing/test_bar.py', 'testing/test_x.py'] },
    });
  });
  it('runtime: a nested opted-in project ignores non-Python-only changes', async () => {
    const result = await runRequiredCheck(await render(), {
      ruffExit: 0,
      docker: false,
      harnessExit: 0,
      changedPath: 'backend/Dockerfile',
    });
    expect(result.status, result.stdout).toBe(0);
    expect(result.log).not.toContain('ruff -m ruff check');
    expect(result.stdout).not.toContain('no Python target');
    expect(result.receipt).toBeUndefined();
  });
  it('runtime: an opted-in root project executes for repo-relative Python changes and fails closed', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'verify-authoring-1',
      workBranch: 'blueprint/verify-authoring-1',
      baseBranch: 'automain',
      owner: 'acme',
      repo: 'python-root',
      testsOnMainBranch: false,
      taskCapability: authoringCapability,
      buildConfig: { directories: [{
        path: '.', type: 'python', lint_cmd: 'ruff check .', test_cmd: 'pytest',
        project_verification: {
          protocol: 'blueprint-v1', harness: 'tools/verified-postgres-tests.sh',
        },
      }] },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    const result = await runRequiredCheck(script, {
      ruffExit: 0,
      docker: false,
      harnessExit: 0,
      changedPath: 'testing/test_x.py',
      harnessPath: 'tools/verified-postgres-tests.sh',
    });
    expect(result.log).toContain('ruff -m ruff check');
    expect(result.status, result.stdout).not.toBe(0);
    expect(result.stdout).toContain('PostgreSQL runtime unavailable');
    expect(result.receipt).toBeUndefined();
    expect(result.capabilityReceipt).toBe(
      `backend_verification:runtime_unavailable:v1:${result.taskId}\n`,
    );
  });
  it('runtime: an opted-in ./backend project executes for repo-relative Python changes and fails closed', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'verify-authoring-1',
      workBranch: 'blueprint/verify-authoring-1',
      baseBranch: 'automain',
      owner: 'acme',
      repo: 'python-service',
      testsOnMainBranch: false,
      taskCapability: authoringCapability,
      buildConfig: { directories: [{
        path: './backend', type: 'python', lint_cmd: 'ruff check .', test_cmd: 'pytest',
        project_verification: {
          protocol: 'blueprint-v1', harness: 'scripts/test-backend.sh',
        },
      }] },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    const result = await runRequiredCheck(script, {
      ruffExit: 0, docker: false, harnessExit: 0, changedPath: 'backend/x.py',
    });
    expect(result.log).toContain('ruff -m ruff check');
    expect(result.status, result.stdout).not.toBe(0);
    expect(result.stdout).toContain('PostgreSQL runtime unavailable');
    expect(result.receipt).toBeUndefined();
    expect(result.capabilityReceipt).toBe(
      `backend_verification:runtime_unavailable:v1:${result.taskId}\n`,
    );
  });
  it.each([
    ['missing Ruff', { ruffExit: 1, docker: true, harnessExit: 0 }],
    ['daemonless Docker', { ruffExit: 0, docker: false, harnessExit: 0 }],
    ['Postgres harness failure', { ruffExit: 0, docker: true, harnessExit: 1 }],
    ['all tests skipped', { ruffExit: 0, docker: true, harnessExit: 0, skipped: 1 }],
  ])('runtime: %s fails closed and cannot mint a green receipt', async (_label, scenario) => {
    const result = await runRequiredCheck(await render(), scenario);
    expect(result.status, result.stdout).not.toBe(0);
    if (result.receipt !== undefined) {
      const receipt = result.receipt as {
        pytest: { exit_code: number };
        lanes?: Record<string, { blocking?: boolean }>;
      };
      expect(receipt.pytest.exit_code).not.toBe(0);
      expect(receipt.lanes?.['backend-pytest']?.blocking).toBe(true);
    }
    expect(result.stdout).toContain('FAIL');
  });
  it('runtime: a blocking backend attribution receipt is retained with all buckets', async () => {
    const result = await runRequiredCheck(await render(), {
      ruffExit: 0,
      docker: true,
      harnessExit: 1,
    });
    expect(result.status, result.stdout).not.toBe(0);
    expect(result.receipt).toMatchObject({
      pytest: {
        state: 'attribution_unavailable',
        current_exit_code: 1,
        exit_code: 1,
        new_failures: {},
        suppressed_failures: {},
        disappeared_failures: {},
        skipped_failures: {},
      },
      lanes: {
        'backend-pytest': {
          state: 'attribution_unavailable',
          blocking: true,
          current_exit_code: 1,
        },
      },
    });
  });
  it.each(['missing', 'syntax-invalid'] as const)(
    'runtime: %s route helper cannot mint backend capability authority', async (routeHelper) => {
      const result = await runRequiredCheck(await render(), {
        ruffExit: 0, docker: true, harnessExit: 0, routeHelper, seedCapabilityReceipt: true,
      });
      expect(result.status, result.stdout).not.toBe(0);
      expect(result.stdout).toContain('route helper unavailable or invalid');
      expect(result.stdout).not.toContain('PostgreSQL runtime unavailable');
      expect(result.receipt).toBeUndefined();
      expect(result.capabilityReceipt).toBeUndefined();
      expect(result.log).not.toContain('harness USE_TEST_POSTGRES=true');
    },
  );
  it('runtime: baked sandbox venv + PG bindir select --sandbox-postgres without Docker', async () => {
    const result = await runRequiredCheck(await render(), {
      ruffExit: 0,
      docker: false,
      harnessExit: 0,
      pgBindir: 'present',
      sandboxPython: true,
    });
    expect(result.status, result.stdout).toBe(0);
    expect(result.log).toContain(
      'harness USE_TEST_POSTGRES=true --sandbox-postgres testing/test_x.py',
    );
    expect(result.receipt).toMatchObject({
      pytest: {
        selectors: ['testing/test_x.py'],
        targeted: true,
        use_test_postgres: true,
        postgres_sidecar: true,
        skipped: 0,
        exit_code: 0,
      },
    });
  });
  it('runtime: PG bindir present but baked venv missing falls back to the Docker --postgres sidecar', async () => {
    const result = await runRequiredCheck(await render(), {
      ruffExit: 0,
      docker: true,
      harnessExit: 0,
      pgBindir: 'present',
      sandboxPython: false,
    });
    expect(result.status, result.stdout).toBe(0);
    expect(result.log).toContain(
      'harness USE_TEST_POSTGRES=true --postgres testing/test_x.py',
    );
    expect(result.log).not.toContain('--sandbox-postgres');
    expect(result.receipt).toMatchObject({
      pytest: {
        selectors: ['testing/test_x.py'],
        targeted: true,
        use_test_postgres: true,
        postgres_sidecar: true,
        skipped: 0,
        exit_code: 0,
      },
    });
  });
  it('runtime: PG bindir present but baked venv missing and no Docker fails closed', async () => {
    const result = await runRequiredCheck(await render(), {
      ruffExit: 0,
      docker: false,
      harnessExit: 0,
      pgBindir: 'present',
      sandboxPython: false,
    });
    expect(result.status, result.stdout).not.toBe(0);
    expect(result.stdout).toContain('PostgreSQL runtime unavailable');
    expect(result.receipt).toBeUndefined();
  });
});

// ── ci_failure closed-loop verify gate ──────────────────────────────────────
// For a ci_failure fix, the EXACT CI-failing pytest selectors must be verified
// passing LOCALLY before ./pr open can push (./pr open gates on ./check). The
// old loop was open: ./check ran lint/typecheck + the standalone-3 smoke files
// but never the actual failing selectors (~3 of ~100 test files), so the fix
// shipped blind and CI re-failed, burning a ~20-min provision+CI iteration.
// The verify block runs the selectors via the sanctioned harness
// (`scripts/test-backend.sh --postgres <selector...>`) — targeted runs that fit
// the agent VM's memory headroom (the full suite OOMs; the harness exempts
// `*.py`/`*::*` args from its memory guard). A still-failing target test lands
// in FAILED so ./check fails closed; Docker-unavailable degrades to a non-
// blocking warn (CI is the backstop).
describe('writeCheckHelper — ci_failure selector verify gate (static)', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const selfRepoCheck = async (
    ciFailureSelectors: string[] | undefined,
    taskId = 'task-cifail-1',
  ): Promise<string> => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
      ciFailureSelectors,
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    return script;
  };

  it('emits a targeted harness run for the exact CI-failing selectors', async () => {
    const script = await selfRepoCheck([
      'testing/test_strategic_spawning.py::test_x',
      'backend/testing/test_release_qualifier.py::test_y',
    ]);
    // The verify block exists and runs ONLY the targeted selectors via the
    // sanctioned harness — never the full suite.
    expect(script).toContain('ci_failure closed-loop verify');
    expect(script).toContain('run "ci_failure selectors verify" bash -c');
    expect(script).toContain('bash scripts/test-backend.sh --postgres "$@"');
    // backend/ prefix stripped (the harness runs pytest from the backend root).
    expect(script).toContain("CI_FAIL_SELECTORS=('testing/test_strategic_spawning.py::test_x' 'testing/test_release_qualifier.py::test_y')");
    // Never the full suite (that OOMs the VM).
    expect(script).not.toContain('bash scripts/test-backend.sh 2>&1');
    expect(script).not.toContain('test-backend.sh --postgres 2>&1'); // no bare full run
  });

  it('runs on plain ./check (not behind WITH_TESTS) so it gates non-LTS-branch ci_failures too', async () => {
    const script = await selfRepoCheck(['testing/test_x.py::test_y']);
    // The verify block lives OUTSIDE the `[ "$WITH_TESTS" = "1" ]` backend
    // pytest gate — a CI failure can occur on a non-LTS branch where the
    // WITH_TESTS gate is off, and ./pr open gates on plain ./check there.
    const verifyAt = script.indexOf('ci_failure closed-loop verify');
    const withTestsAt = script.indexOf('if [ "$WITH_TESTS" = "1" ]');
    expect(verifyAt).toBeGreaterThan(-1);
    // The verify block is appended after the backend `fi`, at top level.
    expect(script).toMatch(/^fi\n\n# ── ci_failure closed-loop verify/m);
    // It is NOT nested inside the WITH_TESTS conditional.
    if (withTestsAt > -1) {
      // The verify block must be the LAST gate emitted (top-level), well after
      // any WITH_TESTS-gated backend pytest run.
      expect(verifyAt).toBeGreaterThan(withTestsAt);
    }
  });

  it('drops shell-unsafe selectors but keeps well-formed ones (graceful degradation)', async () => {
    const script = await selfRepoCheck([
      'testing/test_ok.py::test_good',
      'evil; rm -rf /',
      'testing/test_x.py::test_y && echo pwned',
      "testing/test_x.py::test_'quote",
    ]);
    // Only the well-formed selector survives, single-quote-interpolated.
    expect(script).toContain("CI_FAIL_SELECTORS=('testing/test_ok.py::test_good')");
    // The unsafe payloads never reach the generated bash (scope the negative
    // check to the array literal — `rm -rf` appears in an unrelated header
    // comment about safe worktree cleanup).
    const arrayLine = script.split('\n').find(l => l.includes('CI_FAIL_SELECTORS=('));
    expect(arrayLine).toBeDefined();
    expect(arrayLine).not.toContain('rm -rf');
    expect(arrayLine).not.toContain('echo pwned');
    expect(arrayLine).not.toContain('&&');
    expect(arrayLine).not.toContain(';');
  });

  it('emits NO verify block when there are no ci_failure selectors (non-ci_failure path unchanged)', async () => {
    const script = await selfRepoCheck(undefined);
    expect(script).not.toContain('ci_failure closed-loop verify');
    expect(script).not.toContain('CI_FAIL_SELECTORS');
    // The rest of the self-repo gate is intact.
    expect(script).toContain('PASS — all checks green');
    expect(script).toContain('run "agent vitest (attributed)"');
  });

  it('emits NO verify block when every selector is dropped as unsafe', async () => {
    const script = await selfRepoCheck(['definitely; not a selector', 'app/main.py::nope']);
    expect(script).not.toContain('CI_FAIL_SELECTORS=(');
  });

  it('generated ci_failure-verify ./check passes `bash -n` syntax check', async () => {
    const script = await selfRepoCheck(['testing/test_x.py::test_y', 'testing/test_z.py'], 'task-cifail-bashn');
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });
});

// ── ci_failure selector verify gate — runtime behavior ───────────────────────
// Executes the REAL generated ./check against a `scripts/test-backend.sh` shim
// to prove the closed loop: a still-failing target selector FAILS ./check
// (blocking ./pr open), a passing selector passes ./check and was invoked with
// the exact node-id, and Docker-unavailable degrades to a non-blocking warn.
describe('writeCheckHelper — ci_failure selector verify gate (runtime)', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const selfRepoCheck = async (
    ciFailureSelectors: string[],
    taskId: string,
  ): Promise<string> => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
      ciFailureSelectors,
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    return script;
  };

  // Run the generated ./check under a sandbox whose only meaningful stack is
  // backend (git shim reports a backend/x.py diff). The test-backend.sh shim's
  // exit code + the docker shim's presence are scenario-controlled.
  const runCheck = async (
    script: string,
    scenario: { harnessExit: number; docker: boolean },
    gitShim?: string,
  ): Promise<{ status: number; stdout: string; log: string }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const posix = (p: string): string =>
      p.replace(/\\/g, '/').replace(/^([A-Za-z]):\//, (_m, d: string) => `/${d.toLowerCase()}/`);
    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'cifail-'));
    try {
      const bin = realPath.join(dir, 'bin');
      realFs.mkdirSync(bin, { recursive: true });
      realFs.mkdirSync(realPath.join(dir, 'scripts'), { recursive: true });
      realFs.mkdirSync(realPath.join(dir, 'backend', 'testing'), { recursive: true });
      // git shim: valid repo whose diff vs origin/main touched backend/x.py.
      // A caller-supplied shim replaces it (e.g. to simulate the base diff
      // source being UNAVAILABLE so the gate must fail closed).
      realFs.writeFileSync(realPath.join(bin, 'git'), gitShim ?? `#!/bin/sh
case "$*" in
  *"rev-parse --git-dir"*) echo .git; exit 0 ;;
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'backend/x.py'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      // Make the unrelated self-repo stack blocks inert: no agent/admin-ui
      // diff (git shim returns only backend/x.py), and backend lint deps
      // absent. Provide shims so the standalone gate + size guard no-op-pass.
      realFs.writeFileSync(realPath.join(bin, 'python3'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(realPath.join(bin, 'npm'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(realPath.join(bin, 'sha256sum'), '#!/bin/sh\necho 0  -\n', { mode: 0o755 });
      // test-backend.sh shim: record the exact args it was invoked with, then
      // exit with the scenario's code (simulating selector pass/fail).
      realFs.writeFileSync(
        realPath.join(dir, 'scripts', 'test-backend.sh'),
        `#!/bin/sh\necho "test-backend.sh $*" >> "$LOGFILE"\nexit ${scenario.harnessExit}\n`,
        { mode: 0o755 },
      );
      if (scenario.docker) {
        realFs.writeFileSync(realPath.join(bin, 'docker'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      }
      const scriptPath = realPath.join(dir, 'check');
      realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
      const logfile = realPath.join(dir, 'run.log');
      realFs.writeFileSync(logfile, '');
      const env: Record<string, string> = {
        // Same proven PATH expression as the config-driven runtime harness:
        // sandbox bin first (our shims), inherited PATH after (coreutils/bash).
        PATH: `${posix(bin)}:${process.env.PATH ?? ''}`,
        WORKSPACE: posix(dir),
        LOGFILE: posix(logfile),
        HOME: process.env.HOME ?? '',
      };
      if (!scenario.docker) {
        // Guarantee `command -v docker` is FALSE even on a dev machine with
        // Docker Desktop on PATH. BASH_ENV is sourced by non-interactive bash
        // before the script runs; we override the `command` builtin so that
        // `command -v docker` reports absent while every other lookup falls
        // through to the real builtin — deterministic regardless of host Docker.
        const bashEnv = realPath.join(dir, 'no_docker.sh');
        realFs.writeFileSync(
          bashEnv,
          'command() {\n'
          + '  if [ "$1" = "-v" ] && [ "$2" = "docker" ]; then return 1; fi\n'
          + '  builtin command "$@"\n'
          + '}\n',
          { mode: 0o644 },
        );
        env.BASH_ENV = posix(bashEnv);
      }
      let stdout = '';
      let status = 0;
      try {
        // Plain ./check (no `test` arg) — the verify block must run here, since
        // ./pr open gates on plain ./check on non-LTS branches.
        stdout = realCp.execFileSync('bash', [posix(scriptPath)], {
          cwd: dir,
          env,
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'pipe'],
        });
      } catch (err: unknown) {
        const e = err as { status?: number; stdout?: string; stderr?: string };
        status = e.status ?? -1;
        stdout = (e.stdout ?? '') + (e.stderr ?? '');
      }
      const log = realFs.readFileSync(logfile, 'utf8');
      return { status, stdout, log };
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
    }
  };

  it('blocks ./check (and thus ./pr open) when a target selector still FAILS', async () => {
    const script = await selfRepoCheck(['testing/test_x.py::test_y'], 'rt-fail');
    const r = await runCheck(script, { harnessExit: 1, docker: true });
    // The harness ran with the exact selector...
    expect(r.log).toContain('test-backend.sh --postgres testing/test_x.py::test_y');
    // ...and ./check failed closed because the selector is still red.
    expect(r.status).not.toBe(0);
    expect(r.stdout).toContain('ci_failure selectors verify');
    expect(r.stdout).toContain('FAIL');
  });

  it('passes ./check when the target selector PASSES, invoking the exact node-id(s)', async () => {
    const script = await selfRepoCheck(
      ['testing/test_a.py::test_one', 'testing/test_b.py::test_two'],
      'rt-pass',
    );
    const r = await runCheck(script, { harnessExit: 0, docker: true });
    expect(r.log).toContain('test-backend.sh --postgres testing/test_a.py::test_one testing/test_b.py::test_two');
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('PASS — all checks green');
  });

  it('degrades gracefully (non-blocking warn) when Docker is unavailable', async () => {
    const script = await selfRepoCheck(['testing/test_x.py::test_y'], 'rt-nodocker');
    const r = await runCheck(script, { harnessExit: 1, docker: false });
    // No Docker -> the harness is never invoked...
    expect(r.log).not.toContain('test-backend.sh');
    // ...and ./check does NOT block on an infra artifact (CI is the backstop).
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('Docker unavailable');
    expect(r.stdout).toContain('PASS — all checks green');
  });

  it('check gate fails closed when the base diff source is unavailable', async () => {
    const script = await selfRepoCheck(['testing/test_x.py::test_y'], 'rt-nobase');
    // The generated script must carry the fail-closed guard.
    expect(script).toContain('BASE_DIFF_OK');
    expect(script).toContain('base diff unavailable');
    // A valid repo whose base ref (origin/main AND main) does NOT exist — the
    // cold-clone scenario where the diff source is unavailable. Every git
    // invocation except the repo sanity check fails, so CHANGED can never be
    // computed. This used to swallow the error and exit PASS having run nothing.
    const noBaseShim = `#!/bin/sh
case "$*" in
  *"rev-parse --git-dir"*) echo .git; exit 0 ;;
  *) exit 1 ;;
esac
`;
    const r = await runCheck(script, { harnessExit: 1, docker: false }, noBaseShim);
    // Fail closed, loudly: a silently-empty CHANGED must not read as "PASS".
    expect(r.status).not.toBe(0);
    expect(r.stdout).toContain('base diff source unavailable');
    expect(r.stdout).toContain('FAIL');
    expect(r.stdout).not.toContain('PASS — all checks green');
  });

  it('generated check repairs a shallow clone before failing the base-diff gate', async () => {
    const script = await selfRepoCheck([], 'rt-repair');
    // The generated script must carry the shallow-clone repair: deepen a
    // truncated clone, unshallow, and — the decisive step — fetch the base ref
    // with the EXPLICIT refspec so refs/remotes/origin/main actually exists in a
    // single-branch shallow clone (a plain `git fetch origin main` there only
    // updates FETCH_HEAD, leaving origin/main unresolvable and the gate wedged).
    expect(script).toContain('git fetch --deepen=200 origin "$BASE"');
    expect(script).toContain('git fetch --unshallow origin');
    expect(script).toContain('git fetch origin "$BASE":refs/remotes/origin/$BASE');
    // The base refs do NOT resolve on this (shallow) clone, and only the
    // explicit-refspec fetch creates the tracking ref — so a regression back to
    // the plain fetch form (which never touches .shim_refspec_fetched) fails the
    // re-verify and re-wedges the gate. Every other git call is inert.
    const repairShim = `#!/bin/sh
case "$*" in
  *"rev-parse --git-dir"*) echo .git; exit 0 ;;
  *"rev-parse --verify origin/main"*|*"rev-parse --verify main"*)
    [ -f "$WORKSPACE/.shim_refspec_fetched" ] && exit 0 || exit 1 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'backend/x.py'; exit 0 ;;
  *"fetch --deepen=200"*) exit 0 ;;
  *"fetch --unshallow"*) exit 0 ;;
  *"fetch origin main:refs/remotes/origin/main"*) touch "$WORKSPACE/.shim_refspec_fetched"; exit 0 ;;
  *) exit 0 ;;
esac
`;
    const r = await runCheck(script, { harnessExit: 0, docker: false }, repairShim);
    // The repair turned a would-be fail-closed gate (base refs absent) into a
    // genuine green run scoped to the repaired diff. The "skipping ruff" line
    // only appears inside `if touches backend/` — so it proves CHANGED carried
    // the repaired backend/x.py diff (a failed repair would leave CHANGED empty
    // and the whole backend stack block skipped, then fail closed).
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('PASS — all checks green');
    expect(r.stdout).not.toContain('base diff source unavailable');
    expect(r.stdout).toContain('skipping ruff');
  });
});

// ── attributed backend verification ─────────────────────────────────────────
// The required backend verification fragment resolves the impacted selectors
// and runs them once through the sanctioned Postgres harness plus the shared
// baseline attributor. This keeps inherited failures publishable while keeping
// introduced or unavailable verification states blocking; independent coverage
// checks reject an all-skipped selected run.
describe('writeCheckHelper — attributed backend verification', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('removes the duplicate raw impacted run and retains one attributed selector lane', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'impacted-1',
      workBranch: 'blueprint/impacted-1',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
      taskCapability: { agent_mode: 'write', run_shape: 'workflow', output_kind: 'pr' },
      buildConfig: {
        directories: [{
          path: 'backend',
          type: 'python',
          lint_cmd: 'ruff check .',
          test_cmd: 'python3 -m pytest',
          project_verification: {
            protocol: 'blueprint-v1',
            harness: 'scripts/test-backend.sh',
          },
        }],
      },
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    // The resolver-selected impacted selectors are owned by the required
    // backend verification fragment, which invokes the shared attributor.
    expect(script).toContain('python3 backend/testing/impacted_tests.py');
    expect(script).toContain('python3 scripts/backend_verification_guard.py');
    expect(script.match(/python3 backend\/testing\/impacted_tests\.py/g)).toHaveLength(1);
    // The weaker raw exit-code lane and its independent timeout/harness run are
    // gone; a baseline failure cannot be charged twice by publication.
    expect(script).not.toContain('impacted-test merge-time gate');
    expect(script).not.toContain('IMPACTED_TEST_CAP=8');
    expect(script).not.toContain('FAILED+=("impacted-test gate")');
    expect(script).not.toContain('scripts/test-backend.sh --postgres $_it_run');
    expect(script).toContain('get("blocking", True)');
    expect(script).not.toContain('"preexisting_test_failure" ]; then');
  });

  it('the attributed backend verification always fires for the self-repo (no constant-false guard)', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    // Self-repo with baseBranch !== promotionTarget: shouldRunBackendMergeGate
    // returns true for isBlueprintSelfRepo, so the gate compiles to "1" — never "0".
    writeCheckHelper({
      taskId: 'impacted-off',
      workBranch: 'blueprint/impacted-off',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: false,
      promotionTarget: 'automain',
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    // Gate compiles to "1" — self-repo always gets backend verification.
    expect(script).toContain('[ "$WITH_TESTS" = "1" ] && [ "1" = "1" ]');
    expect(script).not.toContain('[ "$WITH_TESTS" = "1" ] && [ "0" = "1" ]');
  });

  it('emits the attributed backend gate on self-repo automain task PR (baseBranch=automain, promotionTarget=main)', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    // Self-repo automain task: baseBranch='automain', promotionTarget='main' — the
    // old inline ternary evaluated to "0" so the gate never fired on these PRs.
    // shouldRunBackendMergeGate returns true for isBlueprintSelfRepo regardless
    // of the branch relationship, so the gate now compiles to "1".
    writeCheckHelper({
      taskId: 'impacted-automain',
      workBranch: 'blueprint/task-automain',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: false,
      promotionTarget: 'main',
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    // Gate compiles to "1" — not a constant-false guard.
    expect(script).toContain('[ "$WITH_TESTS" = "1" ] && [ "1" = "1" ]');
    expect(script).not.toContain('[ "$WITH_TESTS" = "1" ] && [ "0" = "1" ]');
    // The standalone corpus is routed through structured baseline attribution.
    expect(script).toContain('run "backend pytest (attributed)" python3 scripts/static_release_guard.py');
    expect(script).toContain('--corpus-only --base "$BASE" --repo-root "$WORKSPACE"');
    // The resolver-selected lane is the required attributed verification.
    expect(script).toContain('python3 backend/testing/impacted_tests.py');
  });

  it('generated ./check with self-repo automain+promotionTarget=main passes `bash -n` syntax check', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'impacted-automain-bashn',
      workBranch: 'blueprint/task-automain-bashn',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: false,
      promotionTarget: 'main',
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  it('generated ./check with attributed backend verification passes `bash -n` syntax check', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'impacted-bashn',
      workBranch: 'blueprint/impacted-bashn',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  it('attribution resolver crash is surfaced as a gate failure — not silently swallowed', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'impacted-crash',
      workBranch: 'blueprint/impacted-crash',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    // The raw impacted lane is gone; the retained attributed lane captures
    // resolver failures and charges them to the required verification gate.
    expect(script).not.toContain('python3 backend/testing/impacted_tests.py 2>/dev/null || true');
    expect(script).toContain('_verification_resolver_rc=$?');
    expect(script).toContain('FAILED+=("$_VERIFICATION_DIR verification resolver")');
    // A crash prints stderr content so operators can diagnose.
    expect(script).toContain('cat "$_verification_stderr"');
  });
});

// ── npm_ci_if_lock_changed — lockfile-hash install guard ─────────────────────
// Closes the warm-VM stale-deps hole: a reused VM carries node_modules from a
// prior task, so `[ -d node_modules ] || npm ci` skipped the reinstall even
// when package-lock.json changed on this branch. The new helper hashes the
// lockfile and reinstalls on absence/mismatch. These tests execute the REAL
// generated helper under an `npm` shim (no network, no real install).
describe('writeCheckHelper — npm_ci_if_lock_changed runtime', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const generateSelfRepoScript = async (): Promise<string> => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'lockhash-1',
      workBranch: 'blueprint/lockhash-1',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    return script;
  };

  // Extract just the `npm_ci_if_lock_changed` function definition (header is
  // shared across all stack paths) and run it in a sandbox under an npm shim
  // that records each invocation. Returns whether `npm ci` ran and the
  // sentinel's contents afterward.
  const runHelper = async (
    script: string,
    setup: { lock: string; sentinel?: string },
  ): Promise<{ npmRan: boolean; status: number; sentinel: string | null }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const posix = (p: string): string =>
      p.replace(/\\/g, '/').replace(/^([A-Za-z]):\//, (_m, d: string) => `/${d.toLowerCase()}/`);

    // Pull the helper's body out of the generated header so we test the REAL
    // emitted bash, not a hand-copy.
    const fnMatch = script.match(/npm_ci_if_lock_changed\(\) \{[\s\S]*?\n\}/);
    if (fnMatch === null) throw new Error('npm_ci_if_lock_changed must be defined in the generated header');
    const fnDef = fnMatch[0];

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'lockhash-'));
    try {
      const bin = realPath.join(dir, 'bin');
      realFs.mkdirSync(bin, { recursive: true });
      realFs.mkdirSync(realPath.join(dir, 'node_modules'), { recursive: true });
      const logfile = realPath.join(dir, 'npm.log');
      realFs.writeFileSync(logfile, '');
      // npm shim: record the invocation; `npm ci` always succeeds here.
      realFs.writeFileSync(
        realPath.join(bin, 'npm'),
        '#!/bin/sh\necho "npm $*" >> "$LOGFILE"\nexit 0\n',
        { mode: 0o755 },
      );
      realFs.writeFileSync(realPath.join(dir, 'package-lock.json'), setup.lock);
      const sentinelPath = realPath.join(dir, 'node_modules', '.pkg-lock-hash');
      if (setup.sentinel !== undefined) realFs.writeFileSync(sentinelPath, setup.sentinel);

      // Driver: source the function def, cd into the sandbox, call it.
      const driver = `set -uo pipefail\n${fnDef}\ncd "$SANDBOX" || exit 9\nnpm_ci_if_lock_changed\n`;
      let status = 0;
      try {
        realCp.execFileSync('bash', ['-c', driver], {
          env: {
            PATH: `${posix(bin)}:${process.env.PATH ?? ''}`,
            LOGFILE: posix(logfile),
            SANDBOX: posix(dir),
          },
          stdio: ['ignore', 'pipe', 'pipe'],
        });
      } catch (err: unknown) {
        status = (err as { status?: number }).status ?? -1;
      }
      const log = realFs.readFileSync(logfile, 'utf8');
      const sentinel = realFs.existsSync(sentinelPath)
        ? realFs.readFileSync(sentinelPath, 'utf8')
        : null;
      return { npmRan: log.includes('npm ci'), status, sentinel };
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
    }
  };

  // Compute the sha256 of a lockfile body the same way the helper does, so the
  // "matching" scenario can pre-seed the sentinel with the real hash.
  const sha256Field1 = async (body: string): Promise<string> => {
    const realCrypto = await vi.importActual<typeof import('node:crypto')>('node:crypto');
    return realCrypto.createHash('sha256').update(body).digest('hex');
  };

  it('scenario A: sentinel hash matches the lockfile -> skips npm ci', async () => {
    const script = await generateSelfRepoScript();
    const lock = '{"name":"x","lockfileVersion":3}\n';
    const hash = await sha256Field1(lock);
    const r = await runHelper(script, { lock, sentinel: `${hash}\n` });
    expect(r.status).toBe(0);
    expect(r.npmRan).toBe(false); // matching identity -> no reinstall
    expect(r.sentinel?.trim()).toBe(hash); // sentinel left intact
  });

  it('scenario B: lockfile changed (hash mismatch) -> runs npm ci and rewrites the sentinel', async () => {
    const script = await generateSelfRepoScript();
    const newLock = '{"name":"x","lockfileVersion":3,"changed":true}\n';
    const newHash = await sha256Field1(newLock);
    // Sentinel carries a STALE hash from a prior install (warm-VM scenario).
    const r = await runHelper(script, { lock: newLock, sentinel: 'deadbeefstalehash\n' });
    expect(r.status).toBe(0);
    expect(r.npmRan).toBe(true); // mismatch -> reinstall
    expect(r.sentinel?.trim()).toBe(newHash); // sentinel rewritten to new identity
  });

  it('fail-safe: no sentinel at all -> runs npm ci and writes the sentinel', async () => {
    const script = await generateSelfRepoScript();
    const lock = '{"name":"x","lockfileVersion":3}\n';
    const hash = await sha256Field1(lock);
    const r = await runHelper(script, { lock }); // no sentinel pre-seeded
    expect(r.status).toBe(0);
    expect(r.npmRan).toBe(true);
    expect(r.sentinel?.trim()).toBe(hash);
  });
});

// ── isPytestCmd ───────────────────────────────────────────────────────────────
// True when `lane` is NOT nested inside an `if [ "$WITH_TESTS" = "1" ]` block:
// the most recent WITH_TESTS switch before the lane, if any, was already closed
// by an `fi` before the lane is reached.
function laneIsUnconditional(gate: string, lane: string): boolean {
  const at = gate.indexOf(lane);
  if (at < 0) return false;
  const before = gate.slice(0, at);
  const lastSwitch = before.lastIndexOf('if [ "$WITH_TESTS" = "1" ]');
  if (lastSwitch < 0) return true;
  return /^\s*fi\s*$/m.test(before.slice(lastSwitch));
}

// ── attributed typecheck lanes ───────────────────────────────────────────────
// Both publication gates (the generated self-repo gate and the tracked
// developer default) must route TypeScript through the shared base-attribution
// runner, always-on, so an inherited type error on the integration branch
// cannot block every agent that touches agent/ or admin-ui.
describe('writeCheckHelper — attributed typecheck lanes', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const selfRepoCheck = async (): Promise<string> => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'task-typecheck-lane-1',
      workBranch: 'blueprint/task-typecheck-lane-1',
      baseBranch: 'main',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: true,
    });
    const script = capturedScripts['/tmp/workspace/.check-task'];
    expect(script).toBeDefined();
    return script;
  };

  it('emits attributed typecheck lanes', async () => {
    const script = await selfRepoCheck();

    // Literal pins of the exact emitted commands — not the constants asserted
    // against themselves.
    expect(script).toContain(
      'run "agent tsc (attributed)" python3 scripts/typecheck_verification_guard.py --suite agent --base "$BASE" --repo-root "$WORKSPACE"',
    );
    expect(script).toContain(
      'run "admin-ui typecheck (attributed)" python3 scripts/typecheck_verification_guard.py --suite admin-ui --base "$BASE" --repo-root "$WORKSPACE"',
    );

    // The raw, unattributed compiles are gone from the generated gate.
    expect(script).not.toContain('run "agent tsc" ');
    expect(script).not.toContain("cd agent && npx tsc --noEmit 2>&1");
    expect(script).not.toContain('run "admin-ui typecheck" ');
    expect(script).not.toContain('npm run typecheck 2>&1');

    // Each lane lives inside its own stack block...
    expect(script).toMatch(
      /if touches agent\/; then[\s\S]*run "agent tsc \(attributed\)"/,
    );
    expect(script).toMatch(
      /if admin_ui_runtime_touched; then[\s\S]*run "admin-ui typecheck \(attributed\)"/,
    );
    // ...and always-on: ./pr open gates on plain ./check, so a lane hidden
    // behind the developer-only test switch would leave publication unarmed.
    expect(laneIsUnconditional(script, 'run "agent tsc (attributed)"')).toBe(true);
    expect(laneIsUnconditional(script, 'run "admin-ui typecheck (attributed)"')).toBe(true);
  });

  it('generated ./check with the attributed typecheck lanes passes `bash -n`', async () => {
    const script = await selfRepoCheck();
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  it('routes the tracked developer gate through the same runner, always-on', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const gate = realFs.readFileSync(
      realPath.resolve(process.cwd(), '..', 'scripts', 'check-default.sh'),
      'utf8',
    );

    expect(gate).toContain(
      'run "agent tsc (attributed)" python3 scripts/typecheck_verification_guard.py --suite agent --base "$BASE" --repo-root "$WORKSPACE"',
    );
    expect(gate).toContain(
      'run "admin-ui typecheck (attributed)" python3 scripts/typecheck_verification_guard.py --suite admin-ui --base "$BASE" --repo-root "$WORKSPACE"',
    );
    // The raw whole-project compiles it replaced are gone.
    expect(gate).not.toContain(`run "agent build" bash -c 'cd "agent" && npm run build'`);
    expect(gate).not.toContain(`run "admin-ui typecheck" bash -c 'cd "admin-ui" && npm run typecheck'`);
    // Neither lane sits under the developer-only test switch. The vitest lanes,
    // which genuinely are test-switch-gated, are the control that proves this
    // detector can still say no.
    expect(laneIsUnconditional(gate, 'run "agent tsc (attributed)"')).toBe(true);
    expect(laneIsUnconditional(gate, 'run "admin-ui typecheck (attributed)"')).toBe(true);
    expect(gate).not.toContain('run "admin-ui typecheck (test files)"');
    expect(laneIsUnconditional(gate, 'run "agent vitest (attributed)"')).toBe(false);
    expect(laneIsUnconditional(gate, 'run "admin-ui vitest (attributed)"')).toBe(false);
  });

  it('uses the attributed admin-ui test typecheck lane without a committed snapshot', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const root = realPath.resolve(process.cwd(), '..');

    expect(realFs.existsSync(realPath.join(root, 'admin-ui/scripts/typecheck-tests-baseline.mjs'))).toBe(false);
    expect(realFs.existsSync(realPath.join(root, 'admin-ui/tsconfig.test.baseline.json'))).toBe(false);

    const pkg = JSON.parse(
      realFs.readFileSync(realPath.join(root, 'admin-ui/package.json'), 'utf8'),
    ) as { scripts: Record<string, string> };
    expect(pkg.scripts.typecheck).toBe('tsc -b --noEmit');

    const defaultGate = realFs.readFileSync(
      realPath.join(root, 'scripts/check-default.sh'),
      'utf8',
    );
    expect(defaultGate).toContain(
      'run "admin-ui typecheck (attributed)" python3 scripts/typecheck_verification_guard.py --suite admin-ui',
    );
    expect(defaultGate).not.toContain('typecheck-tests-baseline');
    expect(defaultGate).not.toContain('typecheck (test files)');
  });
});

describe('isPytestCmd', () => {
  it.each([
    ['pytest', true],
    ['pytest -q', true],
    ['pytest -k "not slow"', true],
    ['python -m pytest', true],
    ['python3 -m pytest -x -q', true],
    ['  pytest  ', true],
    ['npm test', false],
    ['go test ./...', false],
    ['bash scripts/test-backend.sh --postgres', false],
    ['pytest-watch', false],
    ['python3 -m unittest', false],
  ])('%s -> %s', async (cmd, expected) => {
    const { isPytestCmd } = await import('./check-helper.js');
    expect(isPytestCmd(cmd)).toBe(expected);
  });
});
