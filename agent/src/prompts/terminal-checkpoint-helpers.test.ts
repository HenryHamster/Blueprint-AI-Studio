import { beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs';

vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    getAgentToken: vi.fn().mockReturnValue(''),
    git: vi.fn().mockReturnValue(''),
  };
});

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

import { writeCodeChangeHelpers } from './code-change-helpers.js';

type PushMode = 'normal' | 'lost-response' | 'competing' | 'remote-unavailable';
type Fixture = {
  bare: string;
  bash: string;
  dir: string;
  env: NodeJS.ProcessEnv;
  git: string;
  helperPath: string;
  payloadLog: string;
  postLog: string;
  repo: string;
  taskId: string;
  workBranch: string;
};

const slash = (value: string): string => value.replace(/\\/g, '/');
let scripts: Record<string, string> = {};
let sequence = 0;

beforeEach(() => {
  scripts = {};
  vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
    const [path, content] = args as [string, string];
    scripts[path] = content;
  });
});

async function makeFixture(helper: 'fail' | 'blocked', mode: PushMode): Promise<Fixture> {
  const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
  const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
  const os = await vi.importActual<typeof import('node:os')>('node:os');
  const path = await vi.importActual<typeof import('node:path')>('node:path');
  const bash = process.platform === 'win32' ? 'C:/Program Files/Git/bin/bash.exe' : 'bash';
  const git = process.platform === 'win32'
    ? 'C:/Program Files/Git/cmd/git.exe'
    : cp.execFileSync(bash, ['-lc', 'command -v git'], { encoding: 'utf8' }).trim();
  if (!path.isAbsolute(git)) throw new Error(`Expected absolute real Git path, got: ${git}`);
  const taskId = `checkpoint-focused-${helper}-${mode}-${process.pid}-${sequence++}`;
  const workBranch = `blueprint/${taskId}`;
  const dir = realFs.mkdtempSync(path.join(os.tmpdir(), 'terminal-checkpoint-focused-'));
  const bare = path.join(dir, 'remote.git');
  const repo = path.join(dir, 'work');
  const bin = path.join(dir, 'bin');
  const postLog = path.join(dir, 'status-post.log');
  const payloadLog = path.join(dir, 'status-payload.jsonl');
  const shimState = path.join(dir, 'push-shim-fired');
  realFs.mkdirSync(repo);
  realFs.mkdirSync(bin);

  const runGit = (cwd: string, args: string[]): string => cp.execFileSync(
    git, args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
  ).trim();
  runGit(dir, ['init', '--bare', bare]);
  runGit(repo, ['init']);
  runGit(repo, ['config', 'user.name', 'Checkpoint Test']);
  runGit(repo, ['config', 'user.email', 'checkpoint@example.test']);
  runGit(repo, ['checkout', '-b', 'main']);
  realFs.writeFileSync(path.join(repo, 'work.txt'), 'base\n');
  runGit(repo, ['add', 'work.txt']);
  runGit(repo, ['commit', '-m', 'base']);
  const baseCommit = runGit(repo, ['rev-parse', 'HEAD']);
  runGit(repo, ['remote', 'add', 'origin', slash(bare)]);
  runGit(repo, ['push', '-u', 'origin', 'main']);

  let competingSha = '';
  if (mode === 'competing') {
    runGit(repo, ['checkout', '-b', 'competitor-seed']);
    realFs.writeFileSync(path.join(repo, 'competitor.txt'), 'do not overwrite\n');
    runGit(repo, ['add', 'competitor.txt']);
    runGit(repo, ['commit', '-m', 'competing checkpoint owner']);
    competingSha = runGit(repo, ['rev-parse', 'HEAD']);
    runGit(repo, ['push', 'origin', `${competingSha}:refs/heads/competitor-seed`]);
    runGit(repo, ['checkout', 'main']);
  }
  realFs.writeFileSync(path.join(repo, 'work.txt'), `base\npending ${helper} work\n`);

  writeCodeChangeHelpers({
    taskId, workBranch, baseBranch: 'main', owner: 'test-owner', repo: 'test-repo',
    testsOnMainBranch: false,
  });
  const helperPath = path.join(repo, helper);
  realFs.writeFileSync(helperPath, scripts[`/tmp/workspace/${helper}`], { mode: 0o755 });
  realFs.writeFileSync(realPath(path, bin, 'git'), `#!/bin/bash
for _arg in "$@"; do
  if [ "$_arg" = "ls-remote" ] && [ "$CHECKPOINT_MODE" = "remote-unavailable" ]; then exit 74; fi
  if [ "$_arg" = "push" ] && [ ! -e "$SHIM_STATE" ]; then
    if [ "$CHECKPOINT_MODE" = "lost-response" ]; then
      : > "$SHIM_STATE"; "$REAL_GIT" "$@" || exit $?; exit 75
    fi
    if [ "$CHECKPOINT_MODE" = "competing" ]; then
      : > "$SHIM_STATE"
      "$REAL_GIT" --git-dir="$BARE_REMOTE" update-ref "refs/heads/$WORK_BRANCH" "$COMPETING_SHA" || exit $?
      exit 75
    fi
  fi
done
exec "$REAL_GIT" "$@"
`, { mode: 0o755 });
  realFs.writeFileSync(realPath(path, bin, 'node'), '#!/bin/bash\n_value=$(cat)\nprintf \'"%s"\' "$_value"\n', { mode: 0o755 });
  realFs.writeFileSync(realPath(path, bin, 'curl'), `#!/bin/bash
for _arg in "$@"; do
  case "$_arg" in */api/agent/github/token*) printf '%s' '{"data":{"token":""}}'; exit 0;; esac
done
_payload=$(cat)
printf '%s\n' "$_payload" >> "$PAYLOAD_LOG"
_remote_sha=$("$REAL_GIT" --git-dir="$BARE_REMOTE" rev-parse "refs/heads/$WORK_BRANCH" 2>/dev/null) || exit 76
printf '%s\n' "$_remote_sha" >> "$POST_LOG"
printf '200'
`, { mode: 0o755 });
  const bashEnv = path.join(dir, 'bash-env.sh');
  realFs.writeFileSync(bashEnv, `git() { "$CHECKPOINT_SHIM_BIN/git" "$@"; }
curl() { "$CHECKPOINT_SHIM_BIN/curl" "$@"; }
node() { "$CHECKPOINT_SHIM_BIN/node" "$@"; }
sleep() { return 0; }
`);
  const env = Object.fromEntries(Object.entries({
    ...process.env,
    ORCHESTRATOR_URL: 'http://orchestrator.test', AGENT_API_TOKEN: 'checkpoint-token',
    REAL_GIT: slash(git), BARE_REMOTE: slash(bare), WORK_BRANCH: workBranch,
    CHECKPOINT_MODE: mode, SHIM_STATE: slash(shimState), COMPETING_SHA: competingSha,
    POST_LOG: slash(postLog), PAYLOAD_LOG: slash(payloadLog), CHECKPOINT_SHIM_BIN: slash(bin),
    BASH_ENV: slash(bashEnv),
  }).filter(([key]) => key.toLowerCase() !== 'path')) as NodeJS.ProcessEnv;
  env.PATH = (process.platform === 'win32'
    ? [bin, 'C:\\Program Files\\Git\\usr\\bin', 'C:\\Program Files\\Git\\bin']
    : [bin, '/usr/bin', '/bin']).join(path.delimiter);
  cp.execFileSync(
    bash, ['-c', 'printf %s "$1" > "$2"', 'checkpoint-test', baseCommit, `/tmp/base_commit_${taskId}`],
    { env, stdio: 'pipe' },
  );
  cp.execFileSync(
    bash, ['-c', 'rm -f "$1"', 'checkpoint-test', `/tmp/${helper}_${taskId}`],
    { env, stdio: 'pipe' },
  );
  return { bare, bash, dir, env, git, helperPath, payloadLog, postLog, repo, taskId, workBranch };
}

function realPath(path: typeof import('node:path'), ...parts: string[]): string {
  return path.join(...parts);
}

async function dispose(fixture: Fixture): Promise<void> {
  const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
  const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
  try {
    cp.execFileSync(
      fixture.bash,
      ['-c', 'rm -f "$@"', 'checkpoint-test', `/tmp/base_commit_${fixture.taskId}`,
        `/tmp/fail_${fixture.taskId}`, `/tmp/blocked_${fixture.taskId}`,
        `/tmp/execution_capability_receipt_${fixture.taskId}`,
        `/tmp/checkpoint_commit_${fixture.taskId}.log`, `/tmp/checkpoint_push_${fixture.taskId}.log`,
        `/tmp/checkpoint_remote_${fixture.taskId}.log`],
      { env: fixture.env, stdio: 'pipe' },
    );
  } finally {
    realFs.rmSync(fixture.dir, { recursive: true, force: true });
  }
}

function payloads(realFs: typeof import('node:fs'), fixture: Fixture): Array<Record<string, unknown>> {
  return realFs.readFileSync(fixture.payloadLog, 'utf8').trim().split(/\r?\n/)
    .filter(Boolean).map(line => JSON.parse(line) as Record<string, unknown>);
}

describe('generated terminal checkpoint helpers with real Git', () => {
  it('checkpoints before status, emits a receipt, advances dirty retry work, and restores the latest diff', async () => {
    const fixture = await makeFixture('fail', 'normal');
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const path = await vi.importActual<typeof import('node:path')>('node:path');
    const git = (cwd: string, args: string[]): string => cp.execFileSync(
      fixture.git, args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
    ).trim();
    try {
      cp.execFileSync(fixture.bash, [fixture.helperPath, 'publication gate unavailable'], {
        cwd: fixture.repo, env: fixture.env, stdio: 'pipe',
      });
      const firstSha = git(fixture.dir, ['--git-dir', fixture.bare, 'rev-parse', `refs/heads/${fixture.workBranch}`]);
      expect(realFs.readFileSync(fixture.postLog, 'utf8').trim()).toBe(firstSha);
      expect(payloads(realFs, fixture)[0]).toMatchObject({
        commit_hash: firstSha, storage_uri: `git-ref:refs/heads/${fixture.workBranch}`,
      });
      expect(payloads(realFs, fixture)[0]).not.toHaveProperty('execution_capability');

      realFs.appendFileSync(path.join(fixture.repo, 'work.txt'), 'latest retry edit\n');
      cp.execFileSync(fixture.bash, ['-c', 'rm -f "$1"', 'checkpoint-test', `/tmp/fail_${fixture.taskId}`], {
        env: fixture.env, stdio: 'pipe',
      });
      cp.execFileSync(fixture.bash, [fixture.helperPath, 'publication gate still unavailable'], {
        cwd: fixture.repo, env: fixture.env, stdio: 'pipe',
      });
      const latestSha = git(fixture.dir, ['--git-dir', fixture.bare, 'rev-parse', `refs/heads/${fixture.workBranch}`]);
      expect(latestSha).not.toBe(firstSha);
      expect(payloads(realFs, fixture)[1]).toMatchObject({
        commit_hash: latestSha, storage_uri: `git-ref:refs/heads/${fixture.workBranch}`,
      });

      const retry = path.join(fixture.dir, 'retry');
      git(fixture.dir, ['clone', '--branch', 'main', fixture.bare, retry]);
      const { prepareTaskWorkBranch } = await import('../task-checkpoint.js');
      const restored = await prepareTaskWorkBranch(
        {
          owner: 'test-owner', repo: 'test-repo', ghToken: 'token', baseBranch: 'main',
          workBranch: fixture.workBranch, taskId: fixture.taskId, ws: null,
        },
        { git: args => git(retry, args), listOpenPullRequestsForBranch: () => Promise.resolve([]), sendLog: () => {} },
      );
      expect(restored.restoredCheckpoint).toBe(true);
      expect(realFs.readFileSync(path.join(retry, 'work.txt'), 'utf8')).toContain('latest retry edit');
      expect(git(retry, ['status', '--porcelain'])).toContain('work.txt');
    } finally {
      await dispose(fixture);
    }
  }, 30_000);

  it('./fail does not trust an exact frame without a check receipt', async () => {
    const fixture = await makeFixture('fail', 'normal');
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    try {
      cp.execFileSync(fixture.bash, [fixture.helperPath,
        '[check] required backend verification state=runtime_unavailable — PostgreSQL runtime unavailable'], {
        cwd: fixture.repo, env: fixture.env, stdio: 'pipe',
      });
      expect(payloads(realFs, fixture)[0]).toMatchObject({
        logs: '[check] required backend verification state=runtime_unavailable — PostgreSQL runtime unavailable',
        failure_category: 'agent_self_failed',
      });
      expect(payloads(realFs, fixture)[0]).not.toHaveProperty('execution_capability');
      expect(realFs.existsSync(fixture.postLog)).toBe(true);
    } finally {
      await dispose(fixture);
    }
  }, 30_000);

  it('./fail consumes the task-bound check receipt and normalizes generic prose', async () => {
    const fixture = await makeFixture('fail', 'normal');
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    try {
      cp.execFileSync(fixture.bash, ['-c', 'printf "%s\\n" "$1" > "$2"', 'receipt-test',
        `backend_verification:runtime_unavailable:v1:${fixture.taskId}`,
        `/tmp/execution_capability_receipt_${fixture.taskId}`], { env: fixture.env, stdio: 'pipe' });
      cp.execFileSync(fixture.bash, [fixture.helperPath, 'verification could not finish'], {
        cwd: fixture.repo, env: fixture.env, stdio: 'pipe',
      });
      expect(payloads(realFs, fixture)[0]).toMatchObject({
        logs: '[check] required backend verification state=runtime_unavailable — PostgreSQL runtime unavailable',
        failure_category: 'agent_self_failed',
        execution_capability: 'backend_verification',
      });
    } finally {
      await dispose(fixture);
    }
  }, 30_000);

  it('./fail ignores a receipt bound to another task', async () => {
    const fixture = await makeFixture('fail', 'normal');
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    try {
      cp.execFileSync(fixture.bash, ['-c', 'printf "%s\\n" "$1" > "$2"', 'receipt-test',
        'backend_verification:runtime_unavailable:v1:another-task',
        `/tmp/execution_capability_receipt_${fixture.taskId}`], { env: fixture.env, stdio: 'pipe' });
      cp.execFileSync(fixture.bash, [fixture.helperPath, 'ordinary implementation failure'], {
        cwd: fixture.repo, env: fixture.env, stdio: 'pipe',
      });
      expect(payloads(realFs, fixture)[0]).toMatchObject({
        logs: 'ordinary implementation failure', failure_category: 'agent_self_failed',
      });
      expect(payloads(realFs, fixture)[0]).not.toHaveProperty('execution_capability');
    } finally {
      await dispose(fixture);
    }
  }, 30_000);

  it('accepts a lost ./blocked push response only when remote equals local', async () => {
    const fixture = await makeFixture('blocked', 'lost-response');
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    try {
      cp.execFileSync(fixture.bash, [fixture.helperPath, 'waiting for publication service'], {
        cwd: fixture.repo, env: fixture.env, stdio: 'pipe',
      });
      const localSha = cp.execFileSync(fixture.git, ['rev-parse', 'HEAD'], {
        cwd: fixture.repo, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
      }).trim();
      expect(realFs.readFileSync(fixture.postLog, 'utf8').trim()).toBe(localSha);
      expect(payloads(realFs, fixture)[0]?.commit_hash).toBe(localSha);
      expect(payloads(realFs, fixture)[0]).not.toHaveProperty('execution_capability');
    } finally {
      await dispose(fixture);
    }
  }, 30_000);

  it('fails closed on a competing SHA without overwrite or terminal POST', async () => {
    const fixture = await makeFixture('fail', 'competing');
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    try {
      expect(() => cp.execFileSync(fixture.bash, [fixture.helperPath, 'cannot publish'], {
        cwd: fixture.repo, env: fixture.env, stdio: 'pipe',
      })).toThrow();
      const remote = cp.execFileSync(
        fixture.git, ['--git-dir', fixture.bare, 'rev-parse', `refs/heads/${fixture.workBranch}`],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
      ).trim();
      const competitor = cp.execFileSync(
        fixture.git, ['--git-dir', fixture.bare, 'rev-parse', 'refs/heads/competitor-seed'],
        { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
      ).trim();
      expect(remote).toBe(competitor);
      expect(realFs.existsSync(fixture.payloadLog)).toBe(false);
    } finally {
      await dispose(fixture);
    }
  }, 30_000);

  it('does not POST terminal state when the initial lease is unavailable', async () => {
    const fixture = await makeFixture('blocked', 'remote-unavailable');
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    try {
      expect(() => cp.execFileSync(fixture.bash, [fixture.helperPath, 'remote unavailable'], {
        cwd: fixture.repo, env: fixture.env, stdio: 'pipe',
      })).toThrow();
      expect(realFs.existsSync(fixture.payloadLog)).toBe(false);
    } finally {
      await dispose(fixture);
    }
  }, 30_000);
});

describe('lifecycle helper help is side-effect free', () => {
  it.each(['fail', 'blocked', 'done', 'pr'] as const)('./%s --help prints usage without Git, network, or markers', async helper => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const os = await vi.importActual<typeof import('node:os')>('node:os');
    const path = await vi.importActual<typeof import('node:path')>('node:path');
    const bash = process.platform === 'win32' ? 'C:/Program Files/Git/bin/bash.exe' : 'bash';
    const taskId = `help-no-side-effect-${helper}-${process.pid}-${sequence++}`;
    writeCodeChangeHelpers({
      taskId, workBranch: `blueprint/${taskId}`, baseBranch: 'main', owner: 'owner', repo: 'repo',
      testsOnMainBranch: false,
    });
    const dir = realFs.mkdtempSync(path.join(os.tmpdir(), 'helper-help-'));
    const script = path.join(dir, helper);
    const sideEffects = path.join(dir, 'side-effects');
    const bashEnv = path.join(dir, 'bash-env.sh');
    realFs.writeFileSync(script, scripts[`/tmp/workspace/${helper}`], { mode: 0o755 });
    realFs.writeFileSync(bashEnv, `git() { echo git >> "$SIDE_EFFECT_LOG"; return 99; }
curl() { echo curl >> "$SIDE_EFFECT_LOG"; return 99; }
node() { echo node >> "$SIDE_EFFECT_LOG"; return 99; }
python3() { echo python3 >> "$SIDE_EFFECT_LOG"; return 99; }
`);
    try {
      const output = cp.execFileSync(bash, [script, '--help'], {
        encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
        env: { ...process.env, BASH_ENV: slash(bashEnv), SIDE_EFFECT_LOG: slash(sideEffects) },
      });
      expect(output).toContain('Usage:');
      expect(realFs.existsSync(sideEffects)).toBe(false);
      expect(realFs.existsSync(`/tmp/${helper}_${taskId}`)).toBe(false);
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
    }
  }, 10_000);

  it('fails closed outside a repository without a terminal POST', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const cp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const os = await vi.importActual<typeof import('node:os')>('node:os');
    const path = await vi.importActual<typeof import('node:path')>('node:path');
    const bash = process.platform === 'win32' ? 'C:/Program Files/Git/bin/bash.exe' : 'bash';
    const taskId = `missing-repo-${process.pid}-${sequence++}`;
    writeCodeChangeHelpers({
      taskId, workBranch: `blueprint/${taskId}`, baseBranch: 'main', owner: 'owner', repo: 'repo',
      testsOnMainBranch: false,
    });
    const dir = realFs.mkdtempSync(path.join(os.tmpdir(), 'helper-missing-repo-'));
    const script = path.join(dir, 'fail');
    const networkLog = path.join(dir, 'network');
    const bashEnv = path.join(dir, 'bash-env.sh');
    realFs.writeFileSync(script, scripts['/tmp/workspace/fail'], { mode: 0o755 });
    realFs.writeFileSync(bashEnv, `git() { return 1; }
curl() { echo curl >> "$NETWORK_LOG"; return 99; }
node() { _value=$(cat); printf '"%s"' "$_value"; }
`);
    try {
      expect(() => cp.execFileSync(bash, [script, 'cannot complete'], {
        stdio: 'pipe',
        env: {
          ...process.env, BASH_ENV: slash(bashEnv), NETWORK_LOG: slash(networkLog),
          ORCHESTRATOR_URL: 'http://orchestrator.test', AGENT_API_TOKEN: 'token',
        },
      })).toThrow();
      expect(realFs.existsSync(networkLog)).toBe(false);
      expect(realFs.existsSync(`/tmp/fail_${taskId}`)).toBe(false);
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
    }
  }, 10_000);
});
