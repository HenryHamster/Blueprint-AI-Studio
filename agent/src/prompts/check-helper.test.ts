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

    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    expect(script).toMatch(/^#!\/bin\/bash/);
    // Blueprint blocks are the default on the self-repo when no buildConfig is supplied.
    expect(script).toContain('if admin_ui_runtime_touched; then');
    expect(script).toContain('touches agent/');
    expect(script).toContain('touches backend/');
    expect(script).toContain('admin-ui playwright chromium');
    // The agent vitest suite runs on every ./check (not behind `test`) for
    // agent-touching changes, routed through `run` so a failure lands in
    // FAILED and ./check fails closed.
    expect(script).toContain(`run "agent vitest" bash -c 'set -o pipefail; cd agent && npx vitest run 2>&1 | tail -80'`);
    // It lives inside the agent stack block, so it only runs when agent/ changed.
    expect(script).toMatch(/if touches agent\/; then[\s\S]*run "agent vitest"/);
    // The standalone backend harness run propagates its failure through the
    // \`| tail\` (same pipefail rationale as the agent vitest line) — without
    // it the gate fails open when the harness exits non-zero.
    expect(script).toContain(`run "backend pytest" bash -c 'set -o pipefail; bash scripts/test-backend.sh --standalone --no-postgres 2>&1 | tail -200'`);
    expect(script).toContain('PASS — all checks green');
    // Size guardrail is wired into the self-repo gate: invoked by reference
    // as the committed checker script, scoped from the touched stacks.
    expect(script).toContain('python3 backend/testing/check_size_limits.py --sections $SIZE_SECTIONS');
    expect(script).toContain('if touches backend/;          then SIZE_SECTIONS="$SIZE_SECTIONS,backend"; fi');
    expect(script).toContain('if touches agent/;            then SIZE_SECTIONS="$SIZE_SECTIONS,agent"; fi');
    expect(script).toContain('if admin_ui_runtime_touched;  then SIZE_SECTIONS="$SIZE_SECTIONS,frontend"; fi');
    // Routed through the `run` helper so a breach lands in FAILED.
    expect(script).toContain('run "size guardrail ($SIZE_SECTIONS)" bash -c');
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
    expect(script).not.toContain('run "agent tsc"');
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
    const script = capturedScripts['/tmp/workspace/check'];
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

  it('routes through buildConfigDrivenStackBlocks when buildConfig.directories is supplied', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'check-direct-3',
      workBranch: 'blueprint/check-direct-3',
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: false,
      buildConfig: {
        directories: [
          { path: 'svc', type: 'python', lint_cmd: 'ruff check .', build_cmd: 'python -m build' },
        ],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
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
    expect(script).not.toContain('run "agent tsc"');
    // Size guardrail is a Blueprint self-repo concern, not config-driven.
    expect(script).not.toContain('check_size_limits.py');
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
});

// ── Config-driven pytest gate — sanctioned harness + GATE-ENVIRONMENT ────────
// Regression guard for the trapped-task bug (22cb9d94 / 6b14ec01, 2026-06-06):
// a configured bare `pytest` ran verbatim on VMs without the Postgres test
// env, silently degrading to in-memory SQLite where ~70 pre-existing
// Postgres-only tests fail regardless of the diff — the mandatory ./pr gate
// then blocked finished, harness-validated work indefinitely.
describe('writeCheckHelper — config-driven pytest gate', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const writeBackendPytestCheck = async (testCmd: string, taskId = 'task-pygate-1'): Promise<string> => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'other-repo',
      testsOnMainBranch: true,
      buildConfig: {
        directories: [{ path: 'backend', type: 'python', test_cmd: testCmd }],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toBeDefined();
    return script;
  };

  it('prefers the sanctioned Docker harness over bare pytest for a top-level directory', async () => {
    const script = await writeBackendPytestCheck('pytest');
    // Harness preference is probed at runtime: file exists + Docker available.
    expect(script).toContain('if [ -f scripts/test-backend.sh ] && command -v docker >/dev/null 2>&1; then');
    // Both harness modes are generated as cases...
    expect(script).toContain(`run "backend test (sanctioned harness)" bash -c 'set -o pipefail; bash scripts/test-backend.sh 2>&1 | tail -200'`);
    expect(script).toContain('MemAvailable');
    expect(script).toContain(`run "backend test (harness standalone)" bash -c 'set -o pipefail; bash scripts/test-backend.sh --standalone --no-postgres 2>&1 | tail -200'`);
  });

  it('prefers the standalone partial over the full suite whenever the harness offers --standalone', async () => {
    // Regression for the 2026-06-07 gate wart: the old probe selected the FULL
    // suite when `MemAvailable >= 4096`, so an 8 GB agent VM (which clears 4 GB
    // at gate-start) ran the full Docker/Postgres suite locally — the run that
    // OOMs (exit 137) and that CI already covers. The local gate is a smoke
    // check; when the harness advertises --standalone, that must win regardless
    // of free memory. The `>= 4096 -> full harness` branch is now reachable only
    // when the harness has NO standalone mode.
    const script = await writeBackendPytestCheck('pytest');
    const standaloneAt = script.indexOf("grep -qs -- '--standalone'");
    const fullByMemAt = script.indexOf('-ge 4096');
    expect(standaloneAt).toBeGreaterThan(-1);
    expect(fullByMemAt).toBeGreaterThan(-1);
    // The --standalone preference is evaluated BEFORE the memory-gated full run.
    expect(standaloneAt).toBeLessThan(fullByMemAt);
    // The full harness locally is now an explicit opt-in only (ALLOW_LOW_MEM),
    // not an incidental consequence of free memory.
    expect(script).toContain('if [ -n "${ALLOW_LOW_MEM:-}" ]; then');
  });

  it('keeps the configured command verbatim for faithful environments (mandatory gate intact)', async () => {
    const script = await writeBackendPytestCheck('pytest');
    // The bare run still exists — taken when the Postgres test env is present
    // or the suite does not select its DB from env at all.
    expect(script).toContain(`run "backend test" bash -c 'cd "backend" && pytest'`);
    // Faithfulness probes: Postgres env detection + env-gated-conftest grep.
    expect(script).toContain('USE_TEST_POSTGRES');
    expect(script).toContain('postgresql*)');
    expect(script).toContain('grep -qs USE_TEST_POSTGRES "backend/conftest.py" "backend"/*/conftest.py');
  });

  it('emits the GATE-ENVIRONMENT marker path instead of an unfaithful SQLite run', async () => {
    const script = await writeBackendPytestCheck('pytest');
    expect(script).toContain('GATE-ENVIRONMENT: backend test gate cannot run faithfully on this VM');
    expect(script).toContain('in-memory SQLite fallback');
    // Marker file ./pr surfaces after a passing gate; cleared fresh per run.
    expect(script).toContain('>> /tmp/check_gate_env_task-pygate-1.txt');
    expect(script).toContain('rm -f /tmp/check_gate_env_task-pygate-1.txt');
    // The skip is advisory-with-evidence, never a silent pass: it points the
    // agent at harness evidence and the structured ./blocked escape hatch.
    expect(script).toContain('record that evidence in the PR body');
    expect(script).toContain('./blocked');
    // The whole gate stays behind `./check test` (WITH_TESTS).
    expect(script).toMatch(/if \[ "\$WITH_TESTS" = "1" \]; then[\s\S]*GATE-ENVIRONMENT/);
  });

  it('routes python -m pytest variants through the honest gate too', async () => {
    const script = await writeBackendPytestCheck('python3 -m pytest -x -q');
    expect(script).toContain('GATE-ENVIRONMENT');
    expect(script).toContain(`run "backend test" bash -c 'cd "backend" && python3 -m pytest -x -q'`);
  });

  it('leaves non-pytest test commands byte-for-byte on the verbatim path', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'task-pygate-2',
      workBranch: 'blueprint/task-pygate-2',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'other-repo',
      testsOnMainBranch: true,
      buildConfig: {
        directories: [{ path: 'web', type: 'node', test_cmd: 'npm test' }],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    expect(script).toContain(`run "web test" bash -c 'cd "web" && npm test'`);
    // No gate machinery for non-pytest commands (the shared header's marker
    // cleanup line mentions GATE-ENVIRONMENT in a comment, so scope the
    // negative checks to the decision ladder + its outputs).
    expect(script).not.toContain('_GATE_MODE');
    expect(script).not.toContain('GATE-ENVIRONMENT:');
    expect(script).not.toContain('test (sanctioned harness)');
  });

  it('skips the harness convention (but keeps faithfulness checks) for nested directory paths', async () => {
    const { writeCheckHelper } = await import('./check-helper.js');
    writeCheckHelper({
      taskId: 'task-pygate-3',
      workBranch: 'blueprint/task-pygate-3',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'other-repo',
      testsOnMainBranch: true,
      buildConfig: {
        directories: [{ path: 'service/api', type: 'python', test_cmd: 'pytest -q' }],
      },
    });
    const script = capturedScripts['/tmp/workspace/check'];
    // scripts/test-service/api.sh would be nonsense — no harness run branch
    // (the prose in the gate-env message may still mention the harness).
    expect(script).not.toContain('scripts/test-service');
    expect(script).not.toContain('test (sanctioned harness)');
    // The faithfulness ladder still applies.
    expect(script).toContain('GATE-ENVIRONMENT');
    expect(script).toContain('grep -qs USE_TEST_POSTGRES "service/api/conftest.py" "service/api"/*/conftest.py');
    expect(script).toContain(`run "service/api test" bash -c 'cd "service/api" && pytest -q'`);
  });

  it('generated pytest-gate ./check passes `bash -n` syntax check', async () => {
    const script = await writeBackendPytestCheck('pytest', 'task-pygate-bashn');
    const { execSync } = await import('child_process');
    execSync('bash -n', { input: script, stdio: ['pipe', 'pipe', 'pipe'] });
  });

  // ── Runtime behavior ────────────────────────────────────────────────────────
  // Executes the REAL generated ./check under shims (no /tmp cross-process
  // handoff, so this runs on every platform — invocations are recorded via an
  // env-passed LOGFILE inside the sandbox dir).
  const runCheck = async (
    script: string,
    scenario: 'gate-env' | 'bare' | 'harness',
  ): Promise<{ status: number; stdout: string; log: string }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'pygate-'));
    // bash on Windows (Git Bash/msys) wants /d/... style paths; no-op on Linux.
    const posix = (p: string): string =>
      p.replace(/\\/g, '/').replace(/^([A-Za-z]):\//, (_m, d: string) => `/${d.toLowerCase()}/`);
    try {
      const bin = realPath.join(dir, 'bin');
      realFs.mkdirSync(bin, { recursive: true });
      realFs.mkdirSync(realPath.join(dir, 'backend', 'testing'), { recursive: true });
      // git shim: valid repo whose diff vs origin/main touched backend/x.py.
      realFs.writeFileSync(realPath.join(bin, 'git'), `#!/bin/sh
case "$*" in
  *"rev-parse --git-dir"*) echo .git; exit 0 ;;
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'backend/x.py'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      // pytest shim: records that the bare command actually ran.
      realFs.writeFileSync(realPath.join(bin, 'pytest'), '#!/bin/sh\necho pytest-ran >> "$LOGFILE"\nexit 0\n', { mode: 0o755 });
      if (scenario === 'gate-env') {
        // Env-gated conftest (the Blueprint shape) + no harness + no Postgres
        // env: a bare run would be the unfaithful SQLite fallback.
        realFs.writeFileSync(realPath.join(dir, 'backend', 'testing', 'conftest.py'), 'USE_TEST_POSTGRES = os.getenv("USE_TEST_POSTGRES")\n');
      } else {
        realFs.writeFileSync(realPath.join(dir, 'backend', 'testing', 'conftest.py'), 'plain conftest\n');
      }
      if (scenario === 'harness') {
        realFs.mkdirSync(realPath.join(dir, 'scripts'), { recursive: true });
        realFs.writeFileSync(realPath.join(dir, 'scripts', 'test-backend.sh'), '#!/bin/sh\necho harness-ran "$@" >> "$LOGFILE"\nexit 0\n', { mode: 0o755 });
        realFs.writeFileSync(realPath.join(bin, 'docker'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      }
      const scriptPath = realPath.join(dir, 'check');
      realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
      const logfile = realPath.join(dir, 'run.log');
      realFs.writeFileSync(logfile, '');
      let stdout = '';
      let status = 0;
      try {
        stdout = realCp.execFileSync('bash', [posix(scriptPath), 'test'], {
          cwd: dir,
          env: {
            PATH: `${posix(bin)}:${process.env.PATH ?? ''}`,
            WORKSPACE: posix(dir),
            LOGFILE: posix(logfile),
            HOME: process.env.HOME ?? '',
          },
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

  it('runtime: env-gated conftest with no harness and no Postgres env -> GATE-ENVIRONMENT skip, never an unfaithful red', async () => {
    const script = await writeBackendPytestCheck('pytest', 'smoke-ge');
    const r = await runCheck(script, 'gate-env');
    expect(r.stdout).toContain('GATE-ENVIRONMENT');
    // The unfaithful SQLite run is skipped, not executed-and-red.
    expect(r.log).not.toContain('pytest-ran');
    // ./check still passes — the marker (surfaced by ./pr) carries the signal.
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('PASS — all checks green');
  });

  it('runtime: non-env-gated suite -> configured pytest runs verbatim (gate stays mandatory)', async () => {
    const script = await writeBackendPytestCheck('pytest', 'smoke-bare');
    const r = await runCheck(script, 'bare');
    expect(r.log).toContain('pytest-ran');
    expect(r.status).toBe(0);
  });

  it('runtime: harness + Docker available -> sanctioned harness runs instead of bare pytest', async () => {
    const script = await writeBackendPytestCheck('pytest', 'smoke-harness');
    const r = await runCheck(script, 'harness');
    expect(r.log).toContain('harness-ran');
    expect(r.log).not.toContain('pytest-ran');
    expect(r.status).toBe(0);
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
    const script = capturedScripts['/tmp/workspace/check'];
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
