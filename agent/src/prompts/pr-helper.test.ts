import { beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs';
import { randomUUID } from 'node:crypto';

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

import { writePrHelper } from './pr-helper.js';

// These are POSIX integration tests: they execFileSync('bash', …) on the
// generated `pr` script, which hands the PR URL back through the hardcoded
// /tmp/pr_url_<task> path (correct for the Linux agent VM). On Windows, Node's
// `/tmp` resolves to a drive path (D:\tmp) while msys-bash writes to its own
// /tmp, so the cross-process file handoff cannot be observed — skip there. The
// suite runs in full on Linux CI.
const describePosix = process.platform === 'win32' ? describe.skip : describe;
const fixtureHeadSha = 'a'.repeat(40);

describePosix('writePrHelper', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const writeScript = (cfg: Parameters<typeof writePrHelper>[0]): string => {
    writePrHelper(cfg);
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    return script;
  };

  const runScript = async (
    script: string,
    args: string[],
    setup: (dir: string, realFs: typeof import('node:fs')) => void,
    envOverrides: NodeJS.ProcessEnv = {},
  ): Promise<{ status: number; stdout: string; stderr: string; checkArgs?: string }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const srRendererSource = realFs.readFileSync(realPath.join(process.cwd(), 'sr-pr-section.js'), 'utf8');
    vi.mocked(fs.readFileSync).mockImplementation((...args: unknown[]) => {
      const [filePath] = args;
      return String(filePath).endsWith('sr-pr-section.js') ? srRendererSource : '';
    });

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'pr-helper-'));
    try {
      setup(dir, realFs);
      const gitPath = realPath.join(dir, 'bin', 'git');
      if (realFs.existsSync(gitPath)) {
        const gitFixture = realFs.readFileSync(gitPath, 'utf8');
        if (!gitFixture.includes('rev-parse HEAD')) {
          realFs.renameSync(gitPath, `${gitPath}.fixture`);
          realFs.writeFileSync(
            gitPath,
            `#!/bin/sh
if [ "$*" = "rev-parse HEAD" ]; then
  printf '%s\\n' '${fixtureHeadSha}'
  exit 0
fi
exec "$(dirname "$0")/git.fixture" "$@"
`,
            { mode: 0o755 },
          );
        }
      }
      const scriptPath = realPath.join(dir, 'pr');
      realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
      try {
        const stdout = realCp.execFileSync('bash', [scriptPath, ...args], {
          cwd: dir,
          env: {
            ...process.env,
            // The generated pr script resolves GATE_TIMEOUT_SECONDS from this
            // env; tests assert the DEFAULT 600s soft / 1800s hard contract, so
            // scrub any operator-level overrides the ambient shell carries.
            PR_GATE_TIMEOUT_SECONDS: '',
            PR_GATE_HARD_TIMEOUT_SECONDS: '',
            WORKSPACE: dir,
            PATH: `${realPath.join(dir, 'bin')}:${process.env.PATH ?? ''}`,
            ORCHESTRATOR_URL: 'https://orchestrator.example',
            AGENT_API_TOKEN: 'token-123',
            ...envOverrides,
          },
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'pipe'],
        });
        const checkArgsPath = realPath.join(dir, 'check.args');
        let checkArgs: string | undefined;
        try {
          checkArgs = realFs.readFileSync(checkArgsPath, 'utf8').trim();
        } catch {
          // File may not exist
        }
        return { status: 0, stdout, stderr: '', checkArgs };
      } catch (err: unknown) {
        const e = err as { status?: number; stdout?: string; stderr?: string };
        return { status: e.status ?? -1, stdout: e.stdout ?? '', stderr: e.stderr ?? '' };
      }
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
    }
  };

  const setupPushRace = (
    dir: string,
    realFs: typeof import('node:fs'),
    taskId: string,
    expectedSha: string,
    observedSha: string,
    localSha: string,
  ): void => {
    const bin = `${dir}/bin`;
    realFs.mkdirSync(bin, { recursive: true });
    realFs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
    realFs.writeFileSync(`${bin}/git`, `#!/bin/sh
STATE="${dir}/ls-remote-count"
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"ls-remote --heads origin refs/heads/blueprint/${taskId}"*)
    N=$(cat "$STATE" 2>/dev/null || printf '0'); N=$((N + 1)); printf '%s' "$N" > "$STATE"
    if [ "$N" = 1 ]; then printf '%s\\t%s\\n' '${expectedSha}' 'refs/heads/blueprint/${taskId}';
    else printf '%s\\t%s\\n' '${observedSha}' 'refs/heads/blueprint/${taskId}'; fi
    exit 0 ;;
  *"push --force-with-lease=refs/heads/blueprint/${taskId}:${expectedSha}"*)
    printf '%s\\n' 'send-pack: connection reset after update' >&2; exit 1 ;;
  *"rev-parse HEAD"*) printf '%s\\n' '${localSha}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    realFs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"; OUT=""
while [ $# -gt 0 ]; do case "$1" in -o) OUT="$2"; shift 2 ;; *) shift ;; esac; done
case "$ARGS" in
  *"/api/agent/heartbeat"*) cat >/dev/null; [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    realFs.writeFileSync(`${bin}/jq`, '#!/bin/sh\ncase "$*" in\n  *".data.token"*) printf \'token-abc\\n\' ;;\n  *".data.html_url"*) printf \'https://github.com/acme/repo/pull/7\\n\' ;;\n  *".mergeable_state"*) printf \'clean\\n\' ;;\n  *".mergeable // \\"null\\""*) printf \'true\\n\' ;;\n  *) printf \'{}\\n\' ;;\nesac\n', { mode: 0o755 });
  };

  it('closes the PR with --superseded-by and writes the canonical PR URL', async () => {
    const script = writeScript({
      taskId: 'task-close-1',
      workBranch: 'blueprint/task-close-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['close', '--pr-number', '42', '--superseded-by', '99'], (dir, realFs) => {
      const bin = `${dir}/bin`;
      realFs.mkdirSync(bin, { recursive: true });
      realFs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/github/token?task_id=task-close-1"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"-X PATCH"*"pulls/42"*) [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/issues/42/comments"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/jq`, `#!/bin/sh
case "$*" in
  *".data.token"*) printf 'token-abc\n' ;;
  *) printf '{}\n' ;;
esac
`, { mode: 0o755 });
    });

    expect(result.status).toBe(0);
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    expect(realFs.readFileSync('/tmp/pr_url_task-close-1', 'utf8').trim()).toBe('https://github.com/acme/repo/pull/42');
  });

  it('forces ./check test on LTS branches when backend paths changed', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const gatePayloadPath = '/tmp/pr_gate_pass_payload_task-lts-1.json';
    realFs.rmSync(gatePayloadPath, { force: true });
    const script = writeScript({
      taskId: 'task-lts-1',
      workBranch: 'blueprint/task-lts-1',
      baseBranch: 'production',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
      promotionTarget: 'production',
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      const bin = `${dir}/bin`;
      realFs.mkdirSync(bin, { recursive: true });
      realFs.writeFileSync(`${dir}/check`, '#!/bin/sh\necho "$*" > "$WORKSPACE/check.args"\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/production"*) exit 0 ;;
  *"diff --name-only origin/production...HEAD"*) printf '%s\n' 'backend/app/main.py'; exit 0 ;;
  *"diff --name-only HEAD"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) printf '%s\n' '?? src/normal.txt'; exit 0 ;;
  *"rev-list --count origin/production..HEAD"*) printf '1\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/task-lts-1"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/heartbeat"*) cat > ${gatePayloadPath}; [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=task-lts-1"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/jq`, '#!/bin/sh\ncase "$*" in\n  *".data.token"*) printf \'token-abc\\n\' ;;\n  *".data.html_url"*) printf \'https://github.com/acme/repo/pull/7\\n\' ;;\n  *".mergeable_state"*) printf \'clean\\n\' ;;\n  *".mergeable // \\"null\\""*) printf \'true\\n\' ;;\n  *) printf \'{}\\n\' ;;\nesac\n', { mode: 0o755 });
    });

    expect(result.status).toBe(0);
    expect(result.checkArgs).toBe('test');
    const gatePayload = JSON.parse(realFs.readFileSync(gatePayloadPath, 'utf8')) as Record<
      string,
      unknown
    >;
    expect(gatePayload).toMatchObject({
      task_id: 'task-lts-1',
      status_message: 'local publication gate passed (passed)',
      publication_gate_receipt: {
        status: 'passed',
        command: './check test',
        exit_code: 0,
      },
    });
    realFs.rmSync(gatePayloadPath, { force: true });
  });

  it('fails fast before push when workflow files are present in the base-relative diff', async () => {
    const script = writeScript({
      taskId: 'task-workflow-1',
      workBranch: 'blueprint/task-workflow-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      const bin = `${dir}/bin`;
      realFs.mkdirSync(bin, { recursive: true });
      realFs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\n' '.github/workflows/ci.yml'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease"*) printf 'push should not run\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/curl`, `#!/bin/sh
exit 0
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/jq`, '#!/bin/sh\nprintf \'{}\\n\'\n', { mode: 0o755 });
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('workflow files changed');
    expect(result.stderr).toContain('.github/workflows/ci.yml');
    expect(result.stderr).not.toContain('push attempt 1');
    expect(result.stderr).not.toContain('push failed after 3 attempts');
  });

  it('fails fast before staging when pending changes include a shell-artifact filename', async () => {
    const script = writeScript({
      taskId: 'task-shell-artifact-1',
      workBranch: 'blueprint/task-shell-artifact-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      const bin = `${dir}/bin`;
      realFs.mkdirSync(bin, { recursive: true });
      realFs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"status --porcelain"*) printf '%s\n' '?? =' '?? src/normal.txt'; exit 0 ;;
  *"add -A"*) printf 'git add should not run\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('Suspicious shell-artifact file(s)');
    expect(result.stderr).toContain('[pr]   =');
    expect(result.stderr).not.toContain('git add should not run');
  });

  it('classifies deterministic workflow-permission push rejections without retrying', async () => {
    const script = writeScript({
      taskId: 'task-workflow-2',
      workBranch: 'blueprint/task-workflow-2',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      const bin = `${dir}/bin`;
      realFs.mkdirSync(bin, { recursive: true });
      realFs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease="*"task-workflow-2"*)
    printf '%s\n' 'remote: refusing to allow a GitHub App to create or update workflow ./.github/workflows/ci.yml without workflows permission.' >&2
    exit 1
    ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/curl`, `#!/bin/sh
exit 0
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/jq`, '#!/bin/sh\nprintf \'{}\\n\'\n', { mode: 0o755 });
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('cannot create or update workflow files without the workflows permission');
    expect(result.stderr).toContain('Revert the workflow file(s) below before pushing');
    expect(result.stderr).not.toContain('push attempt 1');
    expect(result.stderr).not.toContain('push failed after 3 attempts');
  });

  it('reconciles a lost push response only when the remote equals local HEAD', async () => {
    const expectedSha = 'a'.repeat(40);
    const localSha = 'b'.repeat(40);
    const script = writeScript({ taskId: 'task-push-lost', workBranch: 'blueprint/task-push-lost', baseBranch: 'main', owner: 'acme', repo: 'repo', testsOnMainBranch: false });
    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      setupPushRace(dir, realFs, 'task-push-lost', expectedSha, localSha, localSha);
    });

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('exact remote ref equals local HEAD');
  });

  it('refuses to retry when a competing writer advances the remote branch', async () => {
    const expectedSha = 'a'.repeat(40);
    const localSha = 'b'.repeat(40);
    const competingSha = 'c'.repeat(40);
    const script = writeScript({ taskId: 'task-push-race', workBranch: 'blueprint/task-push-race', baseBranch: 'main', owner: 'acme', repo: 'repo', testsOnMainBranch: false });
    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      setupPushRace(dir, realFs, 'task-push-race', expectedSha, competingSha, localSha);
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('remote branch advanced to a competing SHA');
    expect(result.stderr).toContain(competingSha);
  });

  /** Overwrite setupPushRace's git so the second ls-remote reports a checkpoint tip. */
  const setupOwnCheckpointRace = (
    dir: string,
    realFs: typeof import('node:fs'),
    taskId: string,
    expectedSha: string,
    tipSha: string,
    localSha: string,
    tipSubject: string,
  ): void => {
    setupPushRace(dir, realFs, taskId, expectedSha, tipSha, localSha);
    realFs.writeFileSync(`${dir}/bin/git`, `#!/bin/sh
STATE="${dir}/ls-remote-count"
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\n' 'src/app.ts'; exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\n'; exit 0 ;;
  *"ls-remote --heads origin refs/heads/blueprint/${taskId}"*)
    N=$(cat "$STATE" 2>/dev/null || printf '0'); N=$((N + 1)); printf '%s' "$N" > "$STATE"
    if [ "$N" = 1 ]; then printf '%s\t%s\n' '${expectedSha}' 'refs/heads/blueprint/${taskId}';
    else printf '%s\t%s\n' '${tipSha}' 'refs/heads/blueprint/${taskId}'; fi
    exit 0 ;;
  *"push --force-with-lease=refs/heads/blueprint/${taskId}:${expectedSha}"*)
    printf '%s\n' ' ! [rejected] HEAD -> blueprint/${taskId} (stale info)' >&2; exit 1 ;;
  *"push --force-with-lease=refs/heads/blueprint/${taskId}:${tipSha}"*) exit 0 ;;
  *"show -s --format=%s ${tipSha}"*) printf '%s\n' '${tipSubject}'; exit 0 ;;
  *"rev-parse HEAD"*) printf '%s\n' '${localSha}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
  };

  it("adopts this task's own unshipped-work checkpoint as the lease authority", async () => {
    // Regression: the remote tip is the checkpoint THIS task wrote on an earlier
    // attempt's fail-closed path. Treating it as a competing writer stranded
    // finished, check-passing work forever (NeurIPS 37eea260, df1c89d0,
    // 9884994c, 0396aaee). Publication is the step that retires the checkpoint.
    const taskId = 'task-own-ckpt';
    const expectedSha = 'a'.repeat(40);
    const checkpointSha = 'd'.repeat(40);
    const localSha = 'b'.repeat(40);
    const script = writeScript({ taskId, workBranch: `blueprint/${taskId}`, baseBranch: 'main', owner: 'acme', repo: 'repo', testsOnMainBranch: false });
    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      setupOwnCheckpointRace(
        dir, realFs, taskId, expectedSha, checkpointSha, localSha,
        `blueprint-checkpoint: preserve unshipped task ${taskId}`,
      );
    });

    expect(result.stdout).toContain("is this task's own unshipped-work checkpoint");
    expect(result.stdout).toContain(checkpointSha);
    expect(result.stderr).not.toContain('remote branch advanced to a competing SHA');
    expect(result.status).toBe(0);
  });

  it("refuses a checkpoint tip belonging to a different task", async () => {
    // The adoption is keyed on THIS task's id, not the checkpoint prefix: another
    // task's checkpoint is still a competing writer and must fail closed.
    const taskId = 'task-foreign-ckpt';
    const expectedSha = 'a'.repeat(40);
    const foreignSha = 'e'.repeat(40);
    const localSha = 'b'.repeat(40);
    const script = writeScript({ taskId, workBranch: `blueprint/${taskId}`, baseBranch: 'main', owner: 'acme', repo: 'repo', testsOnMainBranch: false });
    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      setupOwnCheckpointRace(
        dir, realFs, taskId, expectedSha, foreignSha, localSha,
        'blueprint-checkpoint: preserve unshipped task some-other-task',
      );
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('remote branch advanced to a competing SHA');
    expect(result.stderr).toContain(foreignSha);
    expect(result.stdout).not.toContain("is this task's own unshipped-work checkpoint");
  });

  it('surfaces the GATE-ENVIRONMENT marker after a passing ./check test', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const markerPath = '/tmp/check_gate_env_task-lts-2.txt';
    realFs.writeFileSync(markerPath, 'backend: pytest gate skipped — no Docker for the sanctioned harness and no Postgres test env\n');
    try {
      const script = writeScript({
        taskId: 'task-lts-2',
        workBranch: 'blueprint/task-lts-2',
        baseBranch: 'production',
        owner: 'acme',
        repo: 'repo',
        testsOnMainBranch: false,
        promotionTarget: 'production',
      });

      const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, fsImpl) => {
        const bin = `${dir}/bin`;
        fsImpl.mkdirSync(bin, { recursive: true });
        fsImpl.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
        fsImpl.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/production"*) exit 0 ;;
  *"diff --name-only origin/production...HEAD"*) printf '%s\n' 'backend/app/main.py'; exit 0 ;;
  *"diff --name-only HEAD"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/production..HEAD"*) printf '1\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/task-lts-2"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
        fsImpl.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/github/token?task_id=task-lts-2"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/8"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/8"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
        fsImpl.writeFileSync(`${bin}/jq`, '#!/bin/sh\ncase "$*" in\n  *".data.token"*) printf \'token-abc\\n\' ;;\n  *".data.html_url"*) printf \'https://github.com/acme/repo/pull/8\\n\' ;;\n  *".mergeable_state"*) printf \'clean\\n\' ;;\n  *".mergeable // \\"null\\""*) printf \'true\\n\' ;;\n  *) printf \'{}\\n\' ;;\nesac\n', { mode: 0o755 });
      });

      expect(result.status).toBe(0);
      // The marker is surfaced verbatim (prefixed) with the evidence guidance.
      expect(result.stdout).toContain('[pr] NOTE: the test gate ran in GATE-ENVIRONMENT mode on this VM:');
      expect(result.stdout).toContain('[pr]   backend: pytest gate skipped');
      expect(result.stdout).toContain('Record your faithful test evidence');
      // It is a note, not a blocker — the PR still ships.
      expect(result.stdout).toContain('[pr] OK — https://github.com/acme/repo/pull/8');
    } finally {
      try { realFs.unlinkSync(markerPath); } catch { /* ignore */ }
    }
  });

  it('exits 4 when the created PR is mergeable=false with state dirty', async () => {
    const script = writeScript({
      taskId: 'task-dirty-1',
      workBranch: 'blueprint/task-dirty-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, realFs) => {
      const bin = `${dir}/bin`;
      realFs.mkdirSync(bin, { recursive: true });
      realFs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"status --porcelain"*) printf '%s\n' '?? src/normal.txt'; exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/task-dirty-1"*) exit 0 ;;
  *"rev-parse --verify origin/main"*) exit 1 ;;
  *"rev-parse --verify main"*) exit 1 ;;
  *"add -A"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/github/token?task_id=task-dirty-1"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":false,"mergeable_state":"dirty"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      realFs.writeFileSync(`${bin}/jq`, `#!/bin/sh
case "$*" in
  *".data.token"*) printf 'token-abc\n' ;;
  *".data.html_url"*) printf 'https://github.com/acme/repo/pull/7\n' ;;
  *".mergeable_state"*) printf 'dirty\n' ;;
  *".mergeable // \\"null\\""*) printf 'false\n' ;;
  *) printf '{}\n' ;;
esac
`, { mode: 0o755 });
    });

    expect(result.status).toBe(4);
    expect(result.stderr).not.toContain('Suspicious shell-artifact file(s)');
    expect(result.stdout).toContain('mergeable=false, mergeable_state=dirty');
  });

  it('retries PR creation on a 503 and honors Retry-After, then succeeds', async () => {
    // The attempt counter lives at an absolute /tmp path keyed on taskId so it
    // survives the cross-process handoff to the spawned bash/curl mocks. PID
    // alone is not a safe uniqueness key here: on the fleet's sandboxed
    // self-hosted runners each job can get its own low-numbered PID namespace,
    // and short-lived vitest/bash/curl processes can genuinely collide on
    // `process.pid` across two jobs that share a /tmp — or an orphaned bash
    // backoff `sleep` from a previously killed run can wake up later and fire
    // one more POST against a reused PID's path. Either way that reads as "one
    // extra POST" here. Mixing in a random UUID makes the path collision-proof
    // regardless of PID reuse.
    const taskId = `task-retry-${process.pid}-${randomUUID()}`;
    const workBranch = `blueprint/${taskId}`;
    const attemptsPath = `/tmp/pr_attempts_${taskId}`;
    const urlPath = `/tmp/pr_url_${taskId}`;
    const script = writeScript({
      taskId,
      workBranch,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    // Cross-process handoff files live at absolute /tmp paths (survive the temp
    // dir cleanup). Start clean so the attempt counter is authoritative.
    realFs.rmSync(attemptsPath, { force: true });
    realFs.rmSync(urlPath, { force: true });

    try {
      const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) printf '%s\\n' '?? src/normal.txt'; exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin ${workBranch}"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      // curl mock: return 503 + Retry-After on the FIRST create POST, then 201.
      // A per-task counter file (absolute /tmp path) tracks attempts across the
      // fresh process spawned for each curl call.
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
DUMP=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    -D) DUMP="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*)
    N=$(cat ${attemptsPath} 2>/dev/null || echo 0)
    N=$((N + 1)); echo "$N" > ${attemptsPath}
    if [ "$N" -lt 2 ]; then
      [ -n "$DUMP" ] && printf 'HTTP/1.1 503 Service Unavailable\\r\\nRetry-After: 1\\r\\n\\r\\n' > "$DUMP"
      [ -n "$OUT" ] && printf '{"error":{"code":503}}' > "$OUT"
      printf '503'; exit 0
    fi
    [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"
    printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/jq`, '#!/bin/sh\ncase "$*" in\n  *".data.token"*) printf \'token-abc\\n\' ;;\n  *".data.html_url"*) printf \'https://github.com/acme/repo/pull/7\\n\' ;;\n  *".mergeable_state"*) printf \'clean\\n\' ;;\n  *".mergeable // \\"null\\""*) printf \'true\\n\' ;;\n  *) printf \'{}\\n\' ;;\nesac\n', { mode: 0o755 });
    });

      expect(result.status).toBe(0);
      // Exactly two POSTs: the 503 (retried) and the 201 (success).
      expect(realFs.readFileSync(attemptsPath, 'utf8').trim()).toBe('2');
      expect(realFs.readFileSync(urlPath, 'utf8').trim()).toBe('https://github.com/acme/repo/pull/7');
    } finally {
      realFs.rmSync(attemptsPath, { force: true });
      realFs.rmSync(urlPath, { force: true });
    }
  });

  // ── jq-free integration: the generated script must work WITHOUT jq on PATH ──
  it('builds valid JSON payload and extracts html_url with jq REMOVED from PATH', async () => {
    const specialTitle = 'fix: title with "quotes", apostrophe\'s, and `backticks`';
    const specialBody = 'Body line 1\nBody line 2 with "double quotes" and \'single quotes\'';

    const script = writeScript({
      taskId: 'task-nojq-1',
      workBranch: 'blueprint/task-nojq-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    // Capture the POST body so we can assert it is valid JSON with the correct fields.
    const result = await runScript(script, ['open', '--title', specialTitle, '--body', specialBody], (dir, realFs) => {
      const bin = `${dir}/bin`;
      realFs.mkdirSync(bin, { recursive: true });
      realFs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      realFs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) printf '%s\\n' '?? src/normal.txt'; exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/task-nojq-1"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      // curl mock: captures the POST body pipe to a file, returns a fake success
      // response with html_url. No jq binary provided — the script must use
      // _json_str for construction and _json_field for parsing.
      realFs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
DUMP=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    -D) DUMP="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/github/token?task_id=task-nojq-1"*)
    printf '{"data":{"token":"token-nojq","html_url":"https://github.com/acme/repo/pull/99"}}'
    exit 0 ;;
  *"/api/agent/github/pull-requests"*)
    # Capture the stdin body that was piped by the script (--data-binary @-).
    cat > /tmp/pr_body_task-nojq-1
    [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/99"}}' > "$OUT"
    printf '201'; exit 0 ;;
  *"/pulls/99"*)
    printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      // DELIBERATELY omit jq from the mock bin — the generated script must NOT
      // depend on it. The _json_str and _json_field functions (python3-based)
      // handle all JSON construction and parsing.
    });

    expect(result.status).toBe(0);

    // (a) Assert the captured PR-creation request body is valid JSON with the
    //     exact expected fields, including special characters round-tripped.
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const bodyRaw = realFs.readFileSync('/tmp/pr_body_task-nojq-1', 'utf8').trim();
    expect(bodyRaw).toBeTruthy();
    const parsed = JSON.parse(bodyRaw) as Record<string, unknown>;
    expect(parsed.task_id).toBe('task-nojq-1');
    expect(parsed.owner).toBe('acme');
    expect(parsed.repo).toBe('repo');
    expect(parsed.title).toBe(specialTitle);
    expect(parsed.head).toBe('blueprint/task-nojq-1');
    expect(parsed.base).toBe('main');
    expect(parsed.expected_head_sha).toBe(fixtureHeadSha);
    expect(parsed.body).toContain(specialBody);
    expect(parsed.body).toContain('- Status: passed');

    // (b) Assert the html_url was extracted (via _json_field) and PR_URL persisted.
    const prUrl = realFs.readFileSync('/tmp/pr_url_task-nojq-1', 'utf8').trim();
    expect(prUrl).toBe('https://github.com/acme/repo/pull/99');

    // Cleanup.
    realFs.rmSync('/tmp/pr_body_task-nojq-1', { force: true });
    realFs.rmSync('/tmp/pr_url_task-nojq-1', { force: true });
  });

  // Read the REAL shared multi-lane receipt writer, so tests that install it
  // exercise the same projection the VM runs rather than a stand-in.
  const readReceiptWriterSource = async (): Promise<string> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const realUrl = await vi.importActual<typeof import('node:url')>('node:url');
    const repoRoot = realPath.resolve(
      realPath.dirname(realUrl.fileURLToPath(import.meta.url)),
      '..', '..', '..',
    );
    return realFs.readFileSync(
      realPath.join(repoRoot, 'scripts', 'verification_receipt.py'),
      'utf8',
    );
  };

  const installReceiptWriter = (
    dir: string,
    rfs: typeof import('node:fs'),
    source: string,
  ): void => {
    rfs.mkdirSync(`${dir}/scripts`, { recursive: true });
    rfs.writeFileSync(`${dir}/scripts/verification_receipt.py`, source);
  };

  const assertSuppressedBaselineReceipt = async (
    receiptShape: 'writer-shaped nested' | 'legacy flat',
    withWriter = false,
  ): Promise<void> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const suffix = withWriter ? '-writer' : '';
    const taskId = (receiptShape === 'writer-shaped nested'
      ? 'task-suppressed-baseline-nested-1'
      : 'task-suppressed-baseline-legacy-1') + suffix;
    const receiptPath = `/tmp/project_verification_receipt_${taskId}.json`;
    const bodyPath = `/tmp/pr_body_${taskId}`;
    const gatePayloadPath = `/tmp/pr_gate_payload_${taskId}.json`;
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(bodyPath, { force: true });
    realFs.rmSync(gatePayloadPath, { force: true });

    const suppressedFailures = {
      'backend/testing/test_z.py::test_inherited::failure': {
        detail: 'AssertionError: inherited failure',
        reason: 'same route/test/failure kind failed on baseline',
      },
      'backend/testing/test_a.py::test_older::error': {
        detail: 'RuntimeError: baseline-only issue',
        reason: 'same route/test/failure kind failed on baseline',
      },
    };
    const receipt = receiptShape === 'writer-shaped nested'
      ? {
          version: 1,
          task_id: taskId,
          directory: '.',
          created_at: '2026-08-17T00:00:00+00:00',
          ruff: {
            command: 'python3 -m ruff check --select E,F,W --ignore E501 <changed-python-files>',
            exit_code: 0,
          },
          pytest: {
            command: 'USE_TEST_POSTGRES=true bash scripts/test-backend.sh <impacted-selectors>',
            harness: 'scripts/test-backend.sh',
            selectors: ['backend/testing/test_z.py', 'backend/testing/test_a.py'],
            targeted: true,
            use_test_postgres: true,
            postgres_sidecar: true,
            state: 'preexisting_test_failure',
            current_exit_code: 1,
            tests: 2,
            skipped: 0,
            failures: 2,
            new_failures: {},
            suppressed_failures: suppressedFailures,
            disappeared_failures: {},
            skipped_failures: {},
            exit_code: 0,
          },
        }
      : {
          state: 'preexisting_test_failure',
          suppressed_failures: suppressedFailures,
        };

    const script = writeScript({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const writerSource = withWriter ? await readReceiptWriterSource() : '';
    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(
        receiptPath,
        JSON.stringify(receipt),
      );
      if (withWriter) {
        installReceiptWriter(dir, rfs, writerSource);
      }
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/${taskId}"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/heartbeat"*) cat > ${gatePayloadPath}; [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) cat > ${bodyPath}; [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    }, { TASK_ID: taskId });

    expect(result.status).toBe(0);
    const body = JSON.parse(realFs.readFileSync(bodyPath, 'utf8')) as { body: string };
    expect(body.body).toContain('- Status: passed');
    expect(body.body).toContain('### Suppressed baseline failures');
    expect(body.body).toContain('`backend/testing/test_a.py::test_older::error`: RuntimeError: baseline-only issue');
    expect(body.body).toContain('`backend/testing/test_z.py::test_inherited::failure`: AssertionError: inherited failure');
    expect(body.body.indexOf('test_a.py')).toBeLessThan(body.body.indexOf('test_z.py'));
    expect(body.body).toContain('<!-- /blueprint:local-gate -->');

    const gatePayload = JSON.parse(realFs.readFileSync(gatePayloadPath, 'utf8')) as {
      publication_gate_receipt: Record<string, unknown>;
    };
    expect(gatePayload.publication_gate_receipt).toMatchObject({
      attribution_state: 'preexisting_test_failure',
      suppressed_failures: suppressedFailures,
      new_failures: {},
      disappeared_failures: {},
      skipped_failures: {},
    });

    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(bodyPath, { force: true });
    realFs.rmSync(gatePayloadPath, { force: true });
    realFs.rmSync(`/tmp/pr_url_${taskId}`, { force: true });
  };

  it.each(['writer-shaped nested', 'legacy flat'] as const)(
    'surfaces suppressed baseline failures from a %s receipt in the local-gate PR section',
    (shape) => assertSuppressedBaselineReceipt(shape),
  );

  // Backward compatibility: a sidecar carrying only the legacy single-lane
  // blocks must render and project EXACTLY as it did before the multi-lane
  // projection existed, even in a repo that ships the shared writer.
  it.each(['writer-shaped nested', 'legacy flat'] as const)(
    'renders a %s single-lane receipt unchanged when the shared multi-lane writer is present',
    (shape) => assertSuppressedBaselineReceipt(shape, true),
  );

  it('projects a pre-guard abort diagnosis into the gate receipt without the shared writer', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const taskId = 'task-pre-guard-abort-lane-1';
    const receiptPath = `/tmp/project_verification_receipt_${taskId}.json`;
    const gatePayloadPath = `/tmp/pr_gate_payload_${taskId}.json`;
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(gatePayloadPath, { force: true });

    const receipt = {
      version: 1,
      lanes: {
        'backend-pytest': {
          lane: 'backend-pytest',
          state: 'route_helper_unavailable',
          blocking: true,
          current_exit_code: 1,
          error: 'route helper unavailable or invalid',
          new_failures: {},
          suppressed_failures: {},
          disappeared_failures: {},
          skipped_failures: {},
        },
      },
    };
    const script = writeScript({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(receiptPath, JSON.stringify(receipt));
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/heartbeat"*) cat > ${gatePayloadPath}; [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    }, { TASK_ID: taskId });

    expect(result.status).toBe(0);
    const gatePayload = JSON.parse(realFs.readFileSync(gatePayloadPath, 'utf8')) as {
      publication_gate_receipt: Record<string, unknown>;
    };
    expect(gatePayload.publication_gate_receipt).toMatchObject({
      attribution_state: 'backend-pytest=route_helper_unavailable',
      new_failures: {},
      suppressed_failures: {},
      disappeared_failures: {},
      skipped_failures: {},
    });

    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(gatePayloadPath, { force: true });
    realFs.rmSync(`/tmp/pr_url_${taskId}`, { force: true });
  });

  // The multi-lane core of the same fallback: attribution_state must name EVERY
  // lane's own state (sorted, '; '-joined) and every folded failure identity must
  // stay namespaced to the lane that produced it. Without this a single lane's
  // verdict silently reads as the whole gate's verdict.
  it('folds every lane state and namespaces each failure identity in the inline fallback', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const taskId = 'task-pre-guard-abort-lane-2';
    const receiptPath = `/tmp/project_verification_receipt_${taskId}.json`;
    const gatePayloadPath = `/tmp/pr_gate_payload_${taskId}.json`;
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(gatePayloadPath, { force: true });

    // Deliberately declared out of sorted order: the fold must sort by lane key.
    const receipt = {
      version: 1,
      lanes: {
        'static-corpus': {
          lane: 'static-corpus',
          state: 'new_failures_present',
          blocking: true,
          new_failures: { 'testing/test_static.py::test_corpus_shape': 'assert 1 == 2' },
          suppressed_failures: {},
          disappeared_failures: {},
          skipped_failures: {},
        },
        'backend-pytest': {
          lane: 'backend-pytest',
          state: 'baseline_attributed',
          blocking: false,
          new_failures: {},
          suppressed_failures: { 'testing/test_api.py::test_legacy': 'known baseline failure' },
          disappeared_failures: {},
          skipped_failures: { 'testing/test_api.py::test_skipped': 'requires postgres' },
        },
      },
    };
    const script = writeScript({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(receiptPath, JSON.stringify(receipt));
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/heartbeat"*) cat > ${gatePayloadPath}; [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/8"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/8"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    }, { TASK_ID: taskId });

    expect(result.status).toBe(0);
    const gatePayload = JSON.parse(realFs.readFileSync(gatePayloadPath, 'utf8')) as {
      publication_gate_receipt: Record<string, unknown>;
    };
    // Sorted by lane key, '; '-joined — not a single lane's state standing in
    // for the whole gate.
    expect(gatePayload.publication_gate_receipt.attribution_state).toBe(
      'backend-pytest=baseline_attributed; static-corpus=new_failures_present',
    );
    expect(gatePayload.publication_gate_receipt).toMatchObject({
      new_failures: { '[static-corpus] testing/test_static.py::test_corpus_shape': 'assert 1 == 2' },
      suppressed_failures: { '[backend-pytest] testing/test_api.py::test_legacy': 'known baseline failure' },
      disappeared_failures: {},
      skipped_failures: { '[backend-pytest] testing/test_api.py::test_skipped': 'requires postgres' },
    });
    // Each folded map carries only the identities of the lane that produced them.
    expect(Object.keys(gatePayload.publication_gate_receipt.new_failures as object)).toEqual([
      '[static-corpus] testing/test_static.py::test_corpus_shape',
    ]);
    expect(Object.keys(gatePayload.publication_gate_receipt.suppressed_failures as object)).toEqual([
      '[backend-pytest] testing/test_api.py::test_legacy',
    ]);

    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(gatePayloadPath, { force: true });
    realFs.rmSync(`/tmp/pr_url_${taskId}`, { force: true });
  });

  // The defect this closes: only the targeted backend lane's suppressions ever
  // reached the PR body and the durable receipt, so a publication forgiven by
  // the static-corpus / Vitest / typecheck lanes read as fully clean.
  it('names every attributed lane\'s suppressed failures in the PR body and the durable receipt', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const taskId = 'task-suppressed-baseline-multilane-1';
    const receiptPath = `/tmp/project_verification_receipt_${taskId}.json`;
    const bodyPath = `/tmp/pr_body_${taskId}`;
    const gatePayloadPath = `/tmp/pr_gate_payload_${taskId}.json`;
    for (const file of [receiptPath, bodyPath, gatePayloadPath]) {
      realFs.rmSync(file, { force: true });
    }

    const lane = (name: string, state: string, suppressed: Record<string, unknown>) => ({
      lane: name,
      state,
      blocking: false,
      new_failures: {},
      suppressed_failures: suppressed,
      disappeared_failures: {},
      skipped_failures: {},
      recorded_at: '2026-08-19T00:00:00+00:00',
    });
    const receipt = {
      version: 1,
      task_id: taskId,
      directory: '.',
      pytest: {
        state: 'preexisting_test_failure',
        new_failures: {},
        suppressed_failures: {
          'backend/testing/test_a.py::test_a::failure': { detail: 'backend inherited' },
        },
        disappeared_failures: {},
        skipped_failures: {},
      },
      lanes: {
        'backend-pytest': lane('backend-pytest', 'preexisting_test_failure', {
          'backend/testing/test_a.py::test_a::failure': { detail: 'backend inherited' },
        }),
        'static-corpus': lane('static-corpus', 'preexisting_test_failure', {
          'backend/testing/test_b.py::test_b::error': { detail: 'corpus inherited' },
        }),
        'vitest:admin-ui': lane('vitest:admin-ui', 'preexisting_test_failure', {
          'src/components/Foo.test.tsx::renders::failure': { detail: 'vitest inherited' },
        }),
        'typecheck:agent': lane('typecheck:agent', 'clean', {}),
      },
    };

    const writerSource = await readReceiptWriterSource();
    const script = writeScript({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      installReceiptWriter(dir, rfs, writerSource);
      rfs.writeFileSync(receiptPath, JSON.stringify(receipt));
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/heartbeat"*) cat > ${gatePayloadPath}; [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) cat > ${bodyPath}; [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    }, { TASK_ID: taskId });

    expect(result.status).toBe(0);
    const body = (JSON.parse(realFs.readFileSync(bodyPath, 'utf8')) as { body: string }).body;
    expect(body).toContain('### Suppressed baseline failures');
    // Each identity is attributed to the lane that forgave it.
    expect(body).toContain('- lane `backend-pytest` (state: preexisting_test_failure)');
    expect(body).toContain('  - `backend/testing/test_a.py::test_a::failure`: backend inherited');
    expect(body).toContain('- lane `static-corpus` (state: preexisting_test_failure)');
    expect(body).toContain('  - `backend/testing/test_b.py::test_b::error`: corpus inherited');
    expect(body).toContain('- lane `vitest:admin-ui` (state: preexisting_test_failure)');
    expect(body).toContain('  - `src/components/Foo.test.tsx::renders::failure`: vitest inherited');
    // A lane that suppressed nothing does not clutter the section.
    expect(body).not.toContain('typecheck:agent');

    const gatePayload = JSON.parse(realFs.readFileSync(gatePayloadPath, 'utf8')) as {
      publication_gate_receipt: Record<string, unknown>;
    };
    expect(gatePayload.publication_gate_receipt).toMatchObject({
      attribution_state:
        'backend-pytest=preexisting_test_failure; static-corpus=preexisting_test_failure; '
        + 'typecheck:agent=clean; vitest:admin-ui=preexisting_test_failure',
      suppressed_failures: {
        '[backend-pytest] backend/testing/test_a.py::test_a::failure': { detail: 'backend inherited' },
        '[static-corpus] backend/testing/test_b.py::test_b::error': { detail: 'corpus inherited' },
        '[vitest:admin-ui] src/components/Foo.test.tsx::renders::failure': { detail: 'vitest inherited' },
      },
      new_failures: {},
      disappeared_failures: {},
      skipped_failures: {},
    });

    for (const file of [receiptPath, bodyPath, gatePayloadPath, `/tmp/pr_url_${taskId}`]) {
      realFs.rmSync(file, { force: true });
    }
  });

  // ── Cross-lane guard-suppressions aggregate ─────────────────────────────────
  // scripts/guard_suppression_receipt.py aggregate merges every guard lane's
  // suppression receipt (static, vitest, typecheck, viewport, backend pytest)
  // into one file. pr-helper.ts's build_guard_suppressions_aggregate and
  // persist_guard_suppressions both shell out to that CLI at the cwd-relative
  // scripts/guard_suppression_receipt.py, so the setup copies the real script
  // into the tmp workspace and seeds per-lane receipts in a directory pinned by
  // BLUEPRINT_GUARD_SUPPRESSION_DIR. Both halves therefore run for real
  // (python3, real execution): aggregate merges the lanes, project writes the
  // durable guard_suppressions key. The node-rendered markdown section is covered end-to-end
  // in guard-suppressions-pr-section.test.ts instead: this suite's top-level
  // `vi.mock('fs', ...)` stubs readFileSync to '', which blanks the base64
  // embed the same way it already does for the sr/screenshots renderers (see
  // the "decodes + invokes the screenshots renderer" test below, which only
  // checks the generated decode/invoke wiring for the same reason).
  it('persists the cross-lane guard-suppressions aggregate onto the heartbeat payload', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const taskId = 'task-guard-suppressions-1';
    const aggregatePath = `/tmp/guard_suppression_aggregate_${taskId}.json`;
    const receiptDir = `/tmp/guard_suppression_receipts_${taskId}`;
    const receiptScriptPath = new URL(
      '../../../scripts/guard_suppression_receipt.py',
      import.meta.url,
    ).pathname;
    const bodyPath = `/tmp/pr_body_${taskId}`;
    const gatePayloadPath = `/tmp/pr_gate_payload_${taskId}.json`;
    realFs.rmSync(receiptDir, { recursive: true, force: true });
    realFs.rmSync(aggregatePath, { force: true });
    realFs.rmSync(bodyPath, { force: true });
    realFs.rmSync(gatePayloadPath, { force: true });

    const emptyBucket = { entries: {}, observed_count: 0, recorded_count: 0, truncated: false };
    const laneReceipts: Record<string, unknown> = {
      static: {
        version: 1,
        lane: 'static',
        task_id: taskId,
        state: 'passed',
        recorded_at: '2026-08-19T00:00:00+00:00',
        buckets: { suppressed: emptyBucket, disappeared: emptyBucket, skipped: emptyBucket },
      },
      vitest: {
        version: 1,
        lane: 'vitest',
        task_id: taskId,
        state: 'preexisting_test_failure',
        recorded_at: '2026-08-19T00:00:00+00:00',
        buckets: {
          // observed_count > recorded entries: the producer capped this bucket,
          // and the durable receipt must not look complete.
          suppressed: {
            entries: { 'vitest::flaky::test': { detail: 'AssertionError: inherited', reason: 'baseline' } },
            observed_count: 4,
            recorded_count: 1,
            truncated: false,
          },
          disappeared: emptyBucket,
          skipped: emptyBucket,
        },
      },
      // The viewport lane's capture-skipped branch records its only identity in
      // `skipped` — a suppressed-only projection would drop it entirely.
      viewport: {
        version: 1,
        lane: 'viewport',
        task_id: taskId,
        state: 'capture_skipped',
        recorded_at: '2026-08-19T00:00:00+00:00',
        buckets: {
          suppressed: emptyBucket,
          disappeared: emptyBucket,
          skipped: {
            entries: { 'admin-ui/src/components/Foo.tsx': { detail: 'chromium unavailable' } },
            observed_count: 1,
            recorded_count: 1,
            truncated: false,
          },
        },
      },
    };

    const script = writeScript({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      // The emitter/projector the generated script shells out to is resolved
      // cwd-relative; copy the real one in so both halves execute for real.
      rfs.mkdirSync(`${dir}/scripts`, { recursive: true });
      rfs.copyFileSync(receiptScriptPath, `${dir}/scripts/guard_suppression_receipt.py`);
      rfs.mkdirSync(receiptDir, { recursive: true });
      for (const [lane, record] of Object.entries(laneReceipts)) {
        rfs.writeFileSync(`${receiptDir}/lane_${lane}.json`, JSON.stringify(record));
      }
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/${taskId}"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/heartbeat"*) cat > ${gatePayloadPath}; [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) cat > ${bodyPath}; [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    }, { TASK_ID: taskId, BLUEPRINT_GUARD_SUPPRESSION_DIR: receiptDir });

    expect(result.status).toBe(0);

    const body = JSON.parse(realFs.readFileSync(bodyPath, 'utf8')) as { body: string };
    expect(body.body).toContain('<!-- /blueprint:local-gate -->');

    // The aggregate half ran for real: every seeded lane is merged into one file.
    const aggregate = JSON.parse(realFs.readFileSync(aggregatePath, 'utf8')) as {
      lanes: Record<string, unknown>;
    };
    expect(Object.keys(aggregate.lanes).sort()).toEqual(['static', 'viewport', 'vitest']);

    const gatePayload = JSON.parse(realFs.readFileSync(gatePayloadPath, 'utf8')) as {
      publication_gate_receipt: { guard_suppressions?: Record<string, unknown> };
    };
    expect(gatePayload.publication_gate_receipt.guard_suppressions).toEqual({
      static: {
        state: 'passed',
        suppressed: {},
        suppressed_observed_count: 0,
        suppressed_truncated: false,
      },
      vitest: {
        state: 'preexisting_test_failure',
        suppressed: { 'vitest::flaky::test': 'AssertionError: inherited' },
        suppressed_observed_count: 4,
        // Producer capped the bucket: observed_count outruns the recorded entries.
        suppressed_truncated: true,
      },
      viewport: {
        state: 'capture_skipped',
        suppressed: {},
        suppressed_observed_count: 0,
        suppressed_truncated: false,
        // A suppressed-only projection would have dropped this lane's only content.
        skipped: { 'admin-ui/src/components/Foo.tsx': 'chromium unavailable' },
        skipped_observed_count: 1,
        skipped_truncated: false,
      },
    });

    realFs.rmSync(receiptDir, { recursive: true, force: true });
    realFs.rmSync(aggregatePath, { force: true });
    realFs.rmSync(bodyPath, { force: true });
    realFs.rmSync(gatePayloadPath, { force: true });
    realFs.rmSync(`/tmp/pr_url_${taskId}`, { force: true });
  });

  it('leaves the PR body and publication verdict unchanged when no guard-suppressions aggregate exists', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const taskId = 'task-guard-suppressions-absent-1';
    const aggregatePath = `/tmp/guard_suppression_aggregate_${taskId}.json`;
    const bodyPath = `/tmp/pr_body_${taskId}`;
    realFs.rmSync(aggregatePath, { force: true });
    realFs.rmSync(bodyPath, { force: true });

    const script = writeScript({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/${taskId}"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/heartbeat"*) cat >/dev/null; [ -n "$OUT" ] && : > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) cat > ${bodyPath}; [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/7"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/7"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
    }, { TASK_ID: taskId });

    expect(result.status).toBe(0);
    const body = JSON.parse(realFs.readFileSync(bodyPath, 'utf8')) as { body: string };
    expect(body.body).not.toContain('blueprint:guard-suppressions');
    expect(body.body).toContain('<!-- /blueprint:local-gate -->');

    realFs.rmSync(bodyPath, { force: true });
    realFs.rmSync(`/tmp/pr_url_${taskId}`, { force: true });
  });

  // ── Zero-effective-diff guard ────────────────────────────────────────────────
  // A rebased/superseded branch can be commits-ahead by SHA yet net to ZERO
  // effective diff vs base. The guard must refuse to push / open a PR and point
  // the agent at the sanctioned no-op exits — NOT report success.
  it('refuses to push or open a PR when commits-ahead but effective diff is empty', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    realFs.rmSync('/tmp/pr_url_task-emptydiff-1', { force: true });

    const script = writeScript({
      taskId: 'task-emptydiff-1',
      workBranch: 'blueprint/task-emptydiff-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      // rev-list reports the branch as 1 commit ahead (rebased/superseded), but
      // the base-relative diff is EMPTY. push + PR-create must NOT run.
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"push --force-with-lease"*) printf 'push should not run\\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
case "$*" in
  *"/api/agent/github/pull-requests"*) printf 'PR create should not run\\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/jq`, '#!/bin/sh\nprintf \'{}\\n\'\n', { mode: 0o755 });
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('ZERO effective diff vs base');
    expect(result.stderr).toContain('./pr close --superseded-by');
    expect(result.stderr).toContain('./done --already-done');
    // Neither the push nor the PR-create seam ran.
    expect(result.stderr).not.toContain('push should not run');
    expect(result.stderr).not.toContain('PR create should not run');
    // No PR URL marker was written.
    expect(realFs.existsSync('/tmp/pr_url_task-emptydiff-1')).toBe(false);
  });

  // ── Failed-vs-empty diff on a depth-limited clone ────────────────────────────
  // On a shallow clone the merge-base commit can be absent, so `git diff
  // base...HEAD` FAILS (non-zero exit, empty stdout) rather than reporting a
  // genuinely-empty diff. The guard must NOT conflate the two: a failed diff is
  // inconclusive, not a proven zero-diff, and must NOT hard-block real work.
  it('does not block on a FAILED base diff (depth-limited clone) — treats it as inconclusive and proceeds', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    realFs.rmSync('/tmp/pr_url_task-shallow-1', { force: true });

    const script = writeScript({
      taskId: 'task-shallow-1',
      workBranch: 'blueprint/task-shallow-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
      // The base-relative diff FAILS (exit 128) as it would when the merge-base
      // is missing on a shallow clone — even after a deepen/unshallow fetch that
      // cannot recover it here. push + PR-create MUST still run (guard skipped).
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf 'fatal: bad revision\\n' >&2; exit 128 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"fetch --deepen"*) exit 0 ;;
  *"fetch --unshallow"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/task-shallow-1"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/github/token?task_id=task-shallow-1"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/8"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/8"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/jq`, '#!/bin/sh\ncase "$*" in\n  *".data.token"*) printf \'token-abc\\n\' ;;\n  *".data.html_url"*) printf \'https://github.com/acme/repo/pull/8\\n\' ;;\n  *".mergeable_state"*) printf \'clean\\n\' ;;\n  *".mergeable // \\"null\\""*) printf \'true\\n\' ;;\n  *) printf \'{}\\n\' ;;\nesac\n', { mode: 0o755 });
    });

    // Behavioral proof the guard did NOT hard-block: under the OLD
    // `2>/dev/null || true` code a failing diff produced empty output and
    // exited 1 with the ZERO-diff message. Here the script reaches status 0
    // (push + PR-create ran) and writes the PR URL marker — the failed diff was
    // treated as inconclusive, not a proven zero-diff. (runScript only surfaces
    // stderr on non-zero exit, so the inconclusive wording is asserted in the
    // static content test below, not here.)
    expect(result.status).toBe(0);
    expect(realFs.existsSync('/tmp/pr_url_task-shallow-1')).toBe(true);
    realFs.rmSync('/tmp/pr_url_task-shallow-1', { force: true });
  });

// Static content checks (no bash execution — these run on every platform,
// including Windows where the POSIX integration suite above is skipped).
  // Regression test: a timed-out ./check must BLOCK ./pr open — not publish with
  // a deferred "authoritative CI must decide" receipt. A missing verdict (check
  // was killed before completion) is not an implicit pass. This is the primary
  // gate-bypass route traced in the incident report for PR #16801 / task b9a990ed.
  it('blocks ./pr open when ./check times out (exit 124) — no-verdict must not be treated as a pass', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const receiptPath = '/tmp/pr_gate_receipt_task-gate-timeout.json';
    const argsPath = '/tmp/pr_gate_timeout_args_task-gate-timeout.txt';
    const prPath = '/tmp/pr_url_task-gate-timeout';
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(argsPath, { force: true });
    realFs.rmSync(prPath, { force: true });

    const script = writeScript({
      taskId: 'task-gate-timeout',
      workBranch: 'blueprint/task-gate-timeout',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nprintf "check must not run\\n" >&2\nexit 99\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/timeout`, `#!/bin/sh
printf '%s\\n' "$*" > ${argsPath}
exit 124
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"push --force-with-lease origin blueprint/task-gate-timeout"*) printf 'push must not run\\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
case "$ARGS" in
  *"/api/agent/github/pull-requests"*) printf 'PR create must not run\\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/jq`, '#!/bin/sh\nprintf \'{}\\n\'\n', { mode: 0o755 });
    });

    // The gate must BLOCK: a timed-out check produced no verdict, which is not a pass.
    expect(result.status).toBe(1);
    expect(result.stdout).not.toContain('check must not run');
    expect(result.stderr).not.toContain('push must not run');
    expect(result.stderr).not.toContain('PR create must not run');
    expect(realFs.readFileSync(argsPath, 'utf8')).toContain(
      '--kill-after=10s 1800s timeout --preserve-status --signal=CONT 600s ./check',
    );
    // Receipt written with timed_out status and authoritative_ci_required: false
    // (we are blocking, not deferring).
    expect(JSON.parse(realFs.readFileSync(receiptPath, 'utf8'))).toMatchObject({
      status: 'timed_out',
      command: './check',
      timeout_seconds: 600,
      hard_timeout_seconds: 1800,
      deadline_extended: false,
      exit_code: 124,
      authoritative_ci_required: false,
    });
    // No PR URL marker written — the gate blocked before push/create.
    expect(realFs.existsSync(prPath)).toBe(false);
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(argsPath, { force: true });
  });

  it('allows a healthy gate to finish after the soft deadline but before the hard bound', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const receiptPath = '/tmp/pr_gate_receipt_task-gate-progress.json';
    const prPath = '/tmp/pr_url_task-gate-progress';
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(prPath, { force: true });

    const script = writeScript({
      taskId: 'task-gate-progress',
      workBranch: 'blueprint/task-gate-progress',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const result = await runScript(
      script,
      ['open', '--title', 'title', '--body', 'body', '--with-tests'],
      (dir, rfs) => {
        const bin = `${dir}/bin`;
        rfs.mkdirSync(bin, { recursive: true });
        rfs.writeFileSync(
          `${dir}/check`,
          '#!/bin/sh\nprintf "%s\\n" "$*" > "$WORKSPACE/check.args"\nsleep 2\nexit 0\n',
          { mode: 0o755 },
        );
        rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) printf '%s\\n' 'M src/app.ts'; exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"remote set-url origin"*) exit 0 ;;
  *"push --force-with-lease origin blueprint/task-gate-progress"*) exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
        rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$ARGS" in
  *"/api/agent/github/token?task_id=task-gate-progress"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/77"}}' > "$OUT"; printf '201'; exit 0 ;;
  *"/pulls/77"*) printf '{"mergeable":true,"mergeable_state":"clean"}'; exit 0 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
        rfs.writeFileSync(`${bin}/jq`, '#!/bin/sh\ncase "$*" in\n  *".data.token"*) printf \'token-abc\\n\' ;;\n  *".data.html_url"*) printf \'https://github.com/acme/repo/pull/77\\n\' ;;\n  *".mergeable_state"*) printf \'clean\\n\' ;;\n  *".mergeable // \\"null\\""*) printf \'true\\n\' ;;\n  *) printf \'{}\\n\' ;;\nesac\n', { mode: 0o755 });
      },
      { PR_GATE_TIMEOUT_SECONDS: '1', PR_GATE_HARD_TIMEOUT_SECONDS: '4' },
    );

    expect(result.status).toBe(0);
    expect(result.stdout).toContain('completed after the soft 1s deadline');
    expect(result.checkArgs).toBe('test');
    expect(JSON.parse(realFs.readFileSync(receiptPath, 'utf8'))).toMatchObject({
      status: 'passed',
      command: './check test',
      timeout_seconds: 1,
      hard_timeout_seconds: 4,
      deadline_extended: true,
      exit_code: 0,
    });
    expect(realFs.existsSync(prPath)).toBe(true);
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(prPath, { force: true });
  }, 15_000);

  it('single-flights overlapping publication and remotely reconciles a completed retry', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const taskId = `task-singleflight-${randomUUID()}`;
    const lockPath = `/tmp/blueprint_pr_open_${taskId}.lock`;
    const ownerPath = `${lockPath}.owner`;
    const legacyReceiptPath = `/tmp/pr_publication_receipt_${taskId}`;
    const selfReviewPath = `/tmp/self_review_${taskId}.json`;
    const prPath = `/tmp/pr_url_${taskId}`;
    const initialHead = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
    const changedHead = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
    const srRendererSource = realFs.readFileSync(
      realPath.join(process.cwd(), 'sr-pr-section.js'),
      'utf8',
    );
    vi.mocked(fs.readFileSync).mockImplementation((...args: unknown[]) => {
      const [filePath] = args;
      return String(filePath).endsWith('sr-pr-section.js') ? srRendererSource : '';
    });
    const script = writeScript({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: true,
    });
    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'pr-singleflight-'));
    const bin = realPath.join(dir, 'bin');
    const scriptPath = realPath.join(dir, 'pr');
    const checkStartedPath = realPath.join(dir, 'check.started');
    const checkCallsPath = realPath.join(dir, 'check.calls');
    const prCallsPath = realPath.join(dir, 'pr.calls');
    realFs.rmSync(ownerPath, { force: true });
    realFs.rmSync(legacyReceiptPath, { force: true });
    realFs.rmSync(selfReviewPath, { force: true });
    realFs.rmSync(prPath, { force: true });

    try {
      realFs.mkdirSync(bin, { recursive: true });
      realFs.writeFileSync(scriptPath, script, { mode: 0o755 });
      realFs.writeFileSync(
        realPath.join(dir, 'check'),
        '#!/bin/sh\nif [ -n "$*" ]; then printf "%s\\n" "$*"; else printf "<none>\\n"; fi >> "$WORKSPACE/check.calls"\n: > "$WORKSPACE/check.started"\nif [ ! -e "$WORKSPACE/check.slow.done" ]; then : > "$WORKSPACE/check.slow.done"; sleep 2; fi\nexit 0\n',
        { mode: 0o755 },
      );
      realFs.writeFileSync(
        realPath.join(bin, 'git'),
        `#!/bin/sh
case "$*" in
  *"rev-parse HEAD"*) cat "$WORKSPACE/head.sha"; exit 0 ;;
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) cat "$WORKSPACE/diff.path"; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only HEAD"*) cat "$WORKSPACE/diff.path"; exit 0 ;;
  *"diff --name-only"*) cat "$WORKSPACE/diff.path"; exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"ls-remote --heads origin refs/heads/blueprint/${taskId}"*) exit 0 ;;
  *"push --force-with-lease="*) exit 0 ;;
  *) exit 0 ;;
esac
`,
        { mode: 0o755 },
      );
      realFs.writeFileSync(
        realPath.join(bin, 'curl'),
        `#!/bin/sh
ARGS="$*"
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
INPUT=$(cat)
case "$ARGS" in
  *"/api/agent/heartbeat"*) [ -n "$OUT" ] && printf '{}' > "$OUT"; printf '200'; exit 0 ;;
  *"/api/agent/github/token?task_id=${taskId}"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*) OUTCOME=create; HTTP=201; if [ -s "$WORKSPACE/pr.calls" ]; then OUTCOME=update; HTTP=200; fi; STATE=$(cat "$WORKSPACE/pr.state"); HEAD=$(cat "$WORKSPACE/head.sha"); printf '%s:%s:%s\\n' "$OUTCOME" "$STATE" "$HEAD" >> "$WORKSPACE/pr.calls"; printf '%s\\n---PAYLOAD---\\n' "$INPUT" >> "$WORKSPACE/pr.payloads"; [ -n "$OUT" ] && printf '{"data":{"html_url":"https://github.com/acme/repo/pull/77"}}' > "$OUT"; printf '%s' "$HTTP"; exit 0 ;;
  *"PATCH"*"/pulls/77"*) printf 'close\\n' >> "$WORKSPACE/close.calls"; [ -n "$OUT" ] && printf '{}' > "$OUT"; printf '200'; exit 0 ;;
  *"/pulls/77"*) STATE=$(cat "$WORKSPACE/pr.state"); if [ "$STATE" = dirty ]; then printf '{"state":"open","merged":false,"mergeable":false,"mergeable_state":"dirty"}'; elif [ "$STATE" = merged ]; then printf '{"state":"closed","merged":true,"mergeable":true,"mergeable_state":"clean"}'; elif [ "$STATE" = closed ]; then printf '{"state":"closed","merged":false,"mergeable":true,"mergeable_state":"clean"}'; else printf '{"state":"open","merged":false,"mergeable":true,"mergeable_state":"clean"}'; fi; exit 0 ;;
  *) exit 0 ;;
esac
`,
        { mode: 0o755 },
      );
      realFs.writeFileSync(
        realPath.join(bin, 'jq'),
        '#!/bin/sh\ncase "$*" in\n  *".data.token"*) printf \'token-abc\\n\' ;;\n  *".data.html_url"*) printf \'https://github.com/acme/repo/pull/77\\n\' ;;\n  *".mergeable_state"*) if [ "$(cat "$WORKSPACE/pr.state")" = dirty ]; then printf \'dirty\\n\'; else printf \'clean\\n\'; fi ;;\n  *".mergeable // \\"null\\""*) if [ "$(cat "$WORKSPACE/pr.state")" = dirty ]; then printf \'false\\n\'; else printf \'true\\n\'; fi ;;\n  *) printf \'{}\\n\' ;;\nesac\n',
        { mode: 0o755 },
      );
      realFs.writeFileSync(realPath.join(dir, 'diff.path'), 'docs/readme.md\n');
      realFs.writeFileSync(realPath.join(dir, 'head.sha'), `${initialHead}\n`);
      realFs.writeFileSync(realPath.join(dir, 'pr.state'), 'open\n');

      const env = {
        ...process.env,
        PR_GATE_TIMEOUT_SECONDS: '10',
        PR_GATE_HARD_TIMEOUT_SECONDS: '15',
        WORKSPACE: dir,
        PATH: `${bin}:${process.env.PATH ?? ''}`,
        ORCHESTRATOR_URL: 'https://orchestrator.example',
        AGENT_API_TOKEN: 'token-123',
      };
      const openArgs = ['open', '--title', 'title', '--body', 'body'];
      const invoke = (
        args: string[] = openArgs,
      ): Promise<{ status: number; stdout: string; stderr: string }> =>
        new Promise((resolve, reject) => {
          const child = realCp.spawn(
            'bash',
            [scriptPath, ...args],
            { cwd: dir, env, stdio: ['ignore', 'pipe', 'pipe'] },
          );
          let stdout = '';
          let stderr = '';
          child.stdout.setEncoding('utf8');
          child.stderr.setEncoding('utf8');
          child.stdout.on('data', (chunk: string) => {
            stdout += chunk;
          });
          child.stderr.on('data', (chunk: string) => {
            stderr += chunk;
          });
          child.on('error', reject);
          child.on('close', (code) => {
            resolve({ status: code ?? -1, stdout, stderr });
          });
        });

      const first = invoke();
      const waitDeadline = Date.now() + 5_000;
      while (!realFs.existsSync(checkStartedPath) && Date.now() < waitDeadline) {
        await new Promise((resolve) => {
          setTimeout(resolve, 25);
        });
      }
      expect(realFs.existsSync(checkStartedPath)).toBe(true);

      const overlapStarted = Date.now();
      const overlapping = await invoke();
      expect(overlapping.status).toBe(75);
      expect(Date.now() - overlapStarted).toBeLessThan(1_000);
      expect(overlapping.stderr).toContain('publication already in progress');
      expect(overlapping.stderr).toContain('no second gate was started');

      const completed = await first;
      expect(completed.status).toBe(0);
      expect(realFs.readFileSync(checkCallsPath, 'utf8').trim().split('\n')).toHaveLength(1);
      expect(realFs.readFileSync(prPath, 'utf8').trim()).toBe(
        'https://github.com/acme/repo/pull/77',
      );
      expect(realFs.readFileSync(prCallsPath, 'utf8').trim().split('\n')).toHaveLength(1);
      expect(realFs.readFileSync(realPath.join(dir, 'pr.payloads'), 'utf8')).toContain(
        `"expected_head_sha":"${initialHead}"`,
      );

      const retried = await invoke();
      expect(retried.status).toBe(0);
      expect(retried.stdout).not.toContain('exact request already published');
      expect(realFs.readFileSync(checkCallsPath, 'utf8').trim().split('\n')).toHaveLength(2);
      expect(realFs.readFileSync(prCallsPath, 'utf8').trim().split('\n')).toHaveLength(2);
      expect(realFs.readFileSync(prCallsPath, 'utf8').trim().split('\n')[1]).toMatch(
        /^update:open:/,
      );

      // A receipt from the reviewed implementation may still exist on a warm
      // worker. It is deliberately ignored: local state cannot authoritatively
      // establish current PR lifecycle, exact head, normalized body, or gate.
      realFs.writeFileSync(
        legacyReceiptPath,
        `${initialHead}\nlegacy-request-hash\nhttps://github.com/acme/repo/pull/77\n`,
      );
      const closed = await invoke(['close', '--pr-number', '77']);
      expect(closed.status).toBe(0);
      expect(realFs.readFileSync(realPath.join(dir, 'close.calls'), 'utf8').trim()).toBe('close');
      expect(realFs.existsSync(legacyReceiptPath)).toBe(true);

      realFs.writeFileSync(realPath.join(dir, 'pr.state'), 'closed\n');
      expect((await invoke()).status).toBe(0);
      // The remote endpoint reconciled the explicit close above. Simulate an
      // independent actor closing it again after that successful reopen.
      realFs.writeFileSync(realPath.join(dir, 'pr.state'), 'open\n');
      realFs.writeFileSync(realPath.join(dir, 'pr.state'), 'closed\n');
      expect((await invoke()).status).toBe(0);
      realFs.writeFileSync(realPath.join(dir, 'pr.state'), 'merged\n');
      expect((await invoke()).status).toBe(0);
      const lifecycleCalls = realFs.readFileSync(prCallsPath, 'utf8');
      expect(lifecycleCalls.match(new RegExp(`closed:${initialHead}`, 'g'))).toHaveLength(2);
      expect(lifecycleCalls).toContain(`merged:${initialHead}`);

      realFs.writeFileSync(realPath.join(dir, 'pr.state'), 'open\n');
      realFs.writeFileSync(realPath.join(dir, 'head.sha'), `${changedHead}\n`);
      expect((await invoke()).status).toBe(0);
      expect(realFs.readFileSync(prCallsPath, 'utf8')).toContain(`open:${changedHead}`);
      expect(realFs.readFileSync(realPath.join(dir, 'pr.payloads'), 'utf8')).toContain(
        `"expected_head_sha":"${changedHead}"`,
      );

      realFs.writeFileSync(
        selfReviewPath,
        JSON.stringify({ verdict: 'unknown', skip_reason: 'dynamic-review-v2' }),
      );
      expect((await invoke()).status).toBe(0);
      expect(realFs.readFileSync(realPath.join(dir, 'pr.payloads'), 'utf8')).toContain(
        'dynamic-review-v2',
      );

      realFs.writeFileSync(realPath.join(dir, 'diff.path'), 'agent/src/index.ts\n');
      expect((await invoke()).status).toBe(0);
      const checkCalls = realFs.readFileSync(checkCallsPath, 'utf8').trim().split('\n');
      expect(checkCalls.at(-1)).toBe('test');
      expect(checkCalls.slice(0, -1)).toContain('<none>');

      realFs.writeFileSync(realPath.join(dir, 'pr.state'), 'dirty\n');
      const conflicted = await invoke();
      expect(conflicted.status).toBe(4);
      expect(conflicted.stdout).toContain('merge conflicts with origin/main');
      expect(realFs.readFileSync(prCallsPath, 'utf8')).toContain(`dirty:${changedHead}`);
      expect(realFs.existsSync(legacyReceiptPath)).toBe(true);
    } finally {
      vi.mocked(fs.readFileSync).mockReturnValue('');
      realFs.rmSync(dir, { recursive: true, force: true });
      realFs.rmSync(ownerPath, { force: true });
      realFs.rmSync(legacyReceiptPath, { force: true });
      realFs.rmSync(selfReviewPath, { force: true });
      realFs.rmSync(prPath, { force: true });
      // The persistent empty flock inode is safe to remove after all children
      // have exited; production keeps it so no unlink/new-inode race exists.
      realFs.rmSync(lockPath, { force: true });
    }
  }, 30_000);

  it('still terminates a genuinely wedged gate at the absolute hard bound', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const receiptPath = '/tmp/pr_gate_receipt_task-gate-wedge.json';
    const prPath = '/tmp/pr_url_task-gate-wedge';
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(prPath, { force: true });

    const script = writeScript({
      taskId: 'task-gate-wedge',
      workBranch: 'blueprint/task-gate-wedge',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const result = await runScript(
      script,
      ['open', '--title', 'title', '--body', 'body'],
      (dir, rfs) => {
        const bin = `${dir}/bin`;
        rfs.mkdirSync(bin, { recursive: true });
        rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nsleep 5\nexit 0\n', { mode: 0o755 });
        rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\\n'; exit 0 ;;
  *"push --force-with-lease"*) printf 'push must not run\\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
        rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
case "$*" in
  *"/api/agent/github/pull-requests"*) printf 'PR create must not run\\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
        rfs.writeFileSync(`${bin}/jq`, '#!/bin/sh\nprintf \'{}\\n\'\n', { mode: 0o755 });
      },
      { PR_GATE_TIMEOUT_SECONDS: '1', PR_GATE_HARD_TIMEOUT_SECONDS: '2' },
    );

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('exceeded its bounded 2s hard deadline');
    expect(result.stderr).not.toContain('push must not run');
    expect(result.stderr).not.toContain('PR create must not run');
    expect(JSON.parse(realFs.readFileSync(receiptPath, 'utf8'))).toMatchObject({
      status: 'timed_out',
      command: './check',
      timeout_seconds: 1,
      hard_timeout_seconds: 2,
      deadline_extended: true,
      exit_code: 124,
    });
    expect(realFs.existsSync(prPath)).toBe(false);
    realFs.rmSync(receiptPath, { force: true });
  }, 15_000);

  it('keeps a deterministic local-gate failure fail-closed', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const receiptPath = '/tmp/pr_gate_receipt_task-gate-failed.json';
    const prPath = '/tmp/pr_url_task-gate-failed';
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(prPath, { force: true });

    const script = writeScript({
      taskId: 'task-gate-failed',
      workBranch: 'blueprint/task-gate-failed',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 23\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/timeout`, `#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    --signal=*|--kill-after=*|--preserve-status) shift ;;
    *s) shift; break ;;
    *) break ;;
  esac
done
exec "$@"
`, { mode: 0o755 });
    });

    expect(result.status).toBe(1);
    expect(result.stdout).toContain('./check failed');
    expect(JSON.parse(realFs.readFileSync(receiptPath, 'utf8'))).toMatchObject({
      status: 'failed',
      command: './check',
      exit_code: 23,
      authoritative_ci_required: false,
    });
    expect(realFs.existsSync(prPath)).toBe(false);
    realFs.rmSync(receiptPath, { force: true });
  });

  // A blocked ./check is RETRYABLE — ./pr open tells the agent to fix the
  // failures and re-run. So the receipt must be persisted through the
  // non-terminal heartbeat channel: posting status=FAILED with
  // failure_category=BLOCKED_PRECONDITION would take the backend's BLOCKED
  // branch, which releases the account slot and deletes the VM the agent is
  // still running on.
  it('records the gate receipt durably when the gate blocks publication', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const receiptPath = '/tmp/pr_gate_receipt_task-gate-durable.json';
    const blockerPath = '/tmp/pr_gate_blocker_task-gate-durable.json';
    const curlArgsPath = '/tmp/pr_gate_blocker_args_task-gate-durable.txt';
    const prPath = '/tmp/pr_url_task-gate-durable';
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(blockerPath, { force: true });
    realFs.rmSync(curlArgsPath, { force: true });
    realFs.rmSync(prPath, { force: true });

    const script = writeScript({
      taskId: 'task-gate-durable',
      workBranch: 'blueprint/task-gate-durable',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nexit 23\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/timeout`, `#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    --signal=*|--kill-after=*|--preserve-status) shift ;;
    *s) shift; break ;;
    *) break ;;
  esac
done
exec "$@"
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
printf '%s\\n' "$@" > ${curlArgsPath}
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
cat > ${blockerPath}
[ -n "$OUT" ] && : > "$OUT"
printf '200'
`, { mode: 0o755 });
    });

    expect(result.status).toBe(1);
    const payload = JSON.parse(realFs.readFileSync(blockerPath, 'utf8')) as Record<
      string,
      unknown
    >;
    expect(payload).toMatchObject({
      task_id: 'task-gate-durable',
      publication_gate_receipt: {
        status: 'failed',
        command: './check',
        timeout_seconds: 600,
        hard_timeout_seconds: 1800,
        exit_code: 23,
        authoritative_ci_required: false,
      },
    });
    // No terminal status transition: these keys would drive the task to
    // BLOCKED and tear down the still-running VM.
    expect(payload.status).toBeUndefined();
    expect(payload.failure_category).toBeUndefined();
    expect(payload.artifact_type).toBeUndefined();
    const curlArgs = realFs.readFileSync(curlArgsPath, 'utf8');
    expect(curlArgs).toContain('/api/agent/heartbeat');
    expect(curlArgs).not.toContain('/api/agent/status');
    expect(realFs.existsSync(prPath)).toBe(false);
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(blockerPath, { force: true });
    realFs.rmSync(curlArgsPath, { force: true });
  });
  // Regression test: when the timeout tool itself fails (exits 127 — command not
  // found inside timeout, or timeout's own internal error), the gate must BLOCK.
  // Previously this fell through the `125|126|127` unavailable case and published
  // with authoritative_ci_required:true, allowing ./done --warn-out-of-scope to
  // complete a submission without ./check ever producing a verdict.
  it('blocks ./pr open when timeout tool exits with a non-zero code without running check', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const receiptPath = '/tmp/pr_gate_receipt_task-gate-unavailable.json';
    const prPath = '/tmp/pr_url_task-gate-unavailable';
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(prPath, { force: true });

    const script = writeScript({
      taskId: 'task-gate-unavailable',
      workBranch: 'blueprint/task-gate-unavailable',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(`${dir}/check`, '#!/bin/sh\nprintf "check must not run\\n" >&2\nexit 99\n', { mode: 0o755 });
      // timeout stub exits 127 — simulates the tool failing before running ./check
      rfs.writeFileSync(`${bin}/timeout`, '#!/bin/sh\nexit 127\n', { mode: 0o755 });
      rfs.writeFileSync(`${bin}/git`, `#!/bin/sh
case "$*" in
  *"rev-parse --verify origin/main"*) exit 0 ;;
  *"diff --name-only origin/main...HEAD"*) printf '%s\n' 'src/app.ts'; exit 0 ;;
  *"diff --name-only --cached"*) exit 0 ;;
  *"diff --name-only"*) exit 0 ;;
  *"ls-files --others --exclude-standard"*) exit 0 ;;
  *"status --porcelain"*) exit 0 ;;
  *"rev-list --count origin/main..HEAD"*) printf '1\n'; exit 0 ;;
  *"push --force-with-lease origin blueprint/task-gate-unavailable"*) printf 'push must not run\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/curl`, `#!/bin/sh
case "$*" in
  *"/api/agent/github/pull-requests"*) printf 'PR create must not run\n' >&2; exit 1 ;;
  *) exit 0 ;;
esac
`, { mode: 0o755 });
      rfs.writeFileSync(`${bin}/jq`, '#!/bin/sh\nprintf \'{}\\n\'\n', { mode: 0o755 });
    });

    // The gate must BLOCK — ./check never ran and produced no verdict.
    expect(result.status).toBe(1);
    expect(result.stdout).not.toContain('check must not run');
    expect(result.stderr).not.toContain('push must not run');
    expect(result.stderr).not.toContain('PR create must not run');
    // Receipt was written (the gate ran and recorded failure).
    expect(JSON.parse(realFs.readFileSync(receiptPath, 'utf8'))).toMatchObject({
      status: 'failed',
      command: './check',
      exit_code: 127,
      authoritative_ci_required: false,
    });
    // No PR URL marker written — gate blocked before push/create.
    expect(realFs.existsSync(prPath)).toBe(false);
    realFs.rmSync(receiptPath, { force: true });
  });

});

// ── Static content: run_pr_gate fallback when timeout is absent ───────────────
// Integration-testing "no timeout in PATH" is unreliable across systems because
// timeout lives at different paths (/usr/bin, /bin via symlink) and PATH
// manipulation can't portably exclude it without breaking other tools. The
// fallback logic is verified here via static content inspection of the generated
// script: the conditional must run ./check DIRECTLY (not set GATE_STATUS=127)
// when timeout is absent, ensuring ./check's exit code determines the gate.
describe('writePrHelper — run_pr_gate timeout-absent fallback (static)', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('runs ./check directly (no GATE_STATUS=127 skip) when timeout is not in PATH', () => {
    writePrHelper({
      taskId: 'task-notimeout-static',
      workBranch: 'blueprint/task-notimeout-static',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();

    // The conditional must check for timeout absence and, when absent, run
    // ./check directly — not set GATE_STATUS=127 and skip running the check.
    expect(script).toContain('if ! command -v timeout >/dev/null 2>&1; then');
    // The fallback branch runs ./check and captures its exit code.
    expect(script).toContain('./check "$@"');
    expect(script).toContain('GATE_STATUS=$?');
    // The WARNING message must be present so the log shows timeout was absent.
    expect(script).toContain('WARNING: timeout not available — running');
    // Old defective pattern (set GATE_STATUS without running check) must be absent.
    expect(script).not.toContain('GATE_STATUS=127');
    // Old pass-through patterns (125|126|127 → return 0) must be absent.
    expect(script).not.toContain('125|126|127');
    expect(script).not.toContain('Publishing with an unavailable receipt');
  });

  it('write_gate_receipt timed_out uses authoritative_ci_required:false — timeout blocks, not defers', () => {
    writePrHelper({
      taskId: 'task-timeout-static',
      workBranch: 'blueprint/task-timeout-static',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();

    // The timed_out receipt must set authoritative_ci_required: false because we
    // are blocking (not deferring). Old code wrote "true" and returned 0 (pass).
    expect(script).toContain('write_gate_receipt timed_out "$GATE_STATUS" false');
    // The old pass-through return (return 0 after timed_out) must be absent.
    // The only return in the 124|137 case must be a non-zero status.
    const timeoutCaseStart = script.indexOf('124|137)');
    const timeoutCaseEnd = script.indexOf(';;', timeoutCaseStart);
    const timeoutBlock = script.slice(timeoutCaseStart, timeoutCaseEnd);
    expect(timeoutBlock).not.toContain('return 0');
    expect(timeoutBlock).toContain('return "$GATE_STATUS"');
    // Error message must explain what happened and how to fix it.
    expect(timeoutBlock).toContain('did not complete within the absolute time limit');
    expect(timeoutBlock).toContain('no verdict was produced');
  });

  it('uses one immutable explicit remote lease and never refreshes it after failure', () => {
    writePrHelper({
      taskId: 'task-exact-lease',
      workBranch: 'blueprint/task-exact-lease',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const script = capturedScripts['/tmp/workspace/pr'];

    expect(script).toContain(
      '_PR_LEASE="--force-with-lease=$_PR_REMOTE_REF:$_PR_EXPECTED_REMOTE_SHA"',
    );
    expect(script).toContain('git push "$_PR_LEASE" origin "HEAD:$_PR_REMOTE_REF"');
    expect(script).toContain('if [ "$_PR_OBSERVED_REMOTE_SHA" != "$_PR_EXPECTED_REMOTE_SHA" ]; then');
    expect(script).not.toContain('stale lease, fetching + retrying');
    expect(script).not.toContain('git fetch origin "blueprint/task-exact-lease"');
  });
});

describe('writePrHelper — generated script content', () => {
  let capturedScripts: Record<string, string> = {};

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  it('owns publication before the local gate and keeps the flock inode stable', () => {
    writePrHelper({
      taskId: 'task-singleflight-static',
      workBranch: 'blueprint/task-singleflight-static',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    expect(script.indexOf('_acquire_pr_open_lock')).toBeLessThan(script.indexOf('run_pr_gate()'));
    expect(script).toContain('flock -n 9');
    expect(script).toContain('return 75');
    expect(script).toContain('no second gate was started');
    expect(script).toContain('refusing publication without a single-flight boundary');
    expect(script).not.toContain('_PR_PUBLICATION_RECEIPT=');
    expect(script).not.toContain('exact request already published');
    expect(script).not.toContain('_pr_open_request_hash');
    expect(script).not.toContain('rm -f "$_PR_OPEN_LOCK_FILE"');
  });

  it('bakes and probes flock in both supported agent images', async () => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const repoRoot = realPath.resolve(process.cwd(), '..');
    const dockerfile = realFs.readFileSync(realPath.join(process.cwd(), 'Dockerfile'), 'utf8');
    const packer = realFs.readFileSync(realPath.join(repoRoot, 'infra', 'packer', 'agent.pkr.hcl'), 'utf8');
    for (const imageSource of [dockerfile, packer]) {
      expect(imageSource).toContain('util-linux');
      expect(imageSource).toContain('command -v flock >/dev/null');
    }
  });

  it('stamps engine-authored commits with classifier-accepted provenance', () => {
    writePrHelper({
      taskId: 'task-provenance-1',
      workBranch: 'blueprint/task-provenance-1',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: false,
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    expect(script).toContain(
      "git -c user.email=blueprint-agent@automated.dev -c user.name='Blueprint Agent' \\",
    );
    expect(script).toContain(
      "commit -m \"$TITLE\" \\\n      -m 'Co-authored-by: Blueprint Agent <blueprint-agent@automated.dev>'",
    );
  });

  it('wires the GATE-ENVIRONMENT marker surfacing into the ./check test branch', () => {
    writePrHelper({
      taskId: 'task-static-1',
      workBranch: 'blueprint/task-static-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    // All gate failures block — a deterministic red, a timeout, or an unavailable
    // tool all prevent ./pr open from writing the PR URL. Only exit 0 passes.
    expect(script).toContain('if ! run_pr_gate test; then');
    expect(script).toContain(
      'timeout --signal=TERM --kill-after=10s "${GATE_HARD_TIMEOUT_SECONDS}s" timeout --preserve-status --signal=CONT "${GATE_TIMEOUT_SECONDS}s" ./check "$@"',
    );
    expect(script).toContain('PR_GATE_TIMEOUT_SECONDS:-600');
    expect(script).toContain('PR_GATE_HARD_TIMEOUT_SECONDS:-1800');
    expect(script).toContain('authoritative_ci_required');
    expect(script).toContain('write_gate_receipt failed "$GATE_STATUS" false');
    expect(script).toContain('<!-- blueprint:local-gate -->');
    expect(script).toContain('if [ "$GATE_STATUS" = "0" ] && [ -s /tmp/check_gate_env_task-static-1.txt ]; then');
    expect(script).toContain('[pr] NOTE: the test gate ran in GATE-ENVIRONMENT mode on this VM:');
    expect(script).toContain("sed 's/^/[pr]   /' /tmp/check_gate_env_task-static-1.txt");
    expect(script).toContain('Record your faithful test evidence');
    expect(script).toContain('classify_push_error()');
    expect(script).toContain('workflow files changed, and this token cannot create or update them without workflows permission');
  });

  it('decodes + invokes the screenshots renderer and idempotently strips its block', () => {
    writePrHelper({
      taskId: 'task-shots-1',
      workBranch: 'blueprint/task-shots-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    // Reads the task-deterministic manifest path the --upload flow publishes.
    expect(script).toContain('SHOTS_MANIFEST="/tmp/viewport_manifest_task-shots-1.json"');
    // Decodes the embedded renderer and runs it with PROJECT_ID from the VM env.
    expect(script).toContain('base64 -d > /tmp/screenshots_section_task-shots-1.js');
    expect(script).toMatch(
      /node \/tmp\/screenshots_section_task-shots-1\.js "\$SHOTS_MANIFEST" "\$\{ORCHESTRATOR_URL:-\}" "\$\{PROJECT_ID:-\}" "task-shots-1"/,
    );
    // Idempotent strip + re-append of the screenshots marker block.
    expect(script).toContain(
      "sed '/<!-- blueprint:screenshots -->/,/<!-- \\/blueprint:screenshots -->/d'",
    );
    expect(script).toContain('BODY=$(printf \'%s\\n\\n%s\' "$BODY" "$SHOTS_SECTION")');
  });

  it('wires the cross-lane guard-suppressions aggregate: builds it, decodes + invokes the renderer, and persists it to the gate receipt', () => {
    writePrHelper({
      taskId: 'task-guard-static-1',
      workBranch: 'blueprint/task-guard-static-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    // Merges every guard lane's receipt via the shared CLI (as a plain
    // subprocess call, not an embedded heredoc — see CLAUDE.md).
    expect(script).toContain('python3 scripts/guard_suppression_receipt.py aggregate --task-id "task-guard-static-1" --out "$GUARD_SUPPRESSIONS_AGGREGATE"');
    expect(script).toContain('GUARD_SUPPRESSIONS_AGGREGATE="/tmp/guard_suppression_aggregate_task-guard-static-1.json"');
    // Decodes the embedded renderer and runs it against the aggregate.
    expect(script).toContain('base64 -d > /tmp/guard_suppressions_section_task-guard-static-1.js');
    expect(script).toContain('node /tmp/guard_suppressions_section_task-guard-static-1.js "$aggregate_file"');
    // Persists the same bounded aggregate onto the durable gate receipt so it
    // reaches task.context via the heartbeat payload.
    expect(script).toContain('"guard_suppressions"');
    expect(script).toContain('persist_guard_suppressions');
    // Section is appended alongside (not instead of) the existing suppressed
    // baseline section — both fold into the one <!-- blueprint:local-gate -->
    // block.
    expect(script).toContain('GATE_SECTION=$(printf \'%s\n%s\' "$GATE_SECTION" "$GUARD_SUPPRESSIONS_SECTION")');
  });

  it('emits the mandatory governed-diff test gate for self-repo task PRs', () => {
    writePrHelper({
      taskId: 'task-selfrepo-1',
      workBranch: 'blueprint/task-selfrepo-1',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: false,
      promotionTarget: 'main',
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    // shouldRunBackendMergeGate returns true for self-repo regardless of branch.
    expect(script).toContain('# Mandatory governed-diff gate: force WITH_TESTS=1 when the diff touches');
    expect(script).toContain("grep -Eq '^(backend/|agent/|admin-ui/|scripts/)'");
    expect(script).toContain('governed path touched — forcing WITH_TESTS=1 (mandatory publication gate)');
    expect(script).toContain('WITH_TESTS=1');
  });

  it('selects the governed test path for non-backend publication diffs', () => {
    writePrHelper({
      taskId: 'task-selfrepo-agent-1',
      workBranch: 'blueprint/task-selfrepo-agent-1',
      baseBranch: 'automain',
      owner: 'HenryHamster',
      repo: 'BlueprintAIStudio',
      testsOnMainBranch: false,
      promotionTarget: 'main',
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    expect(script).toContain("grep -Eq '^(backend/|agent/|admin-ui/|scripts/)'");
    expect(script).not.toContain("grep -q '^backend/'");
  });

  it('does NOT emit the mandatory test gate for a foreign repo off promotion target (baseBranch=automain, promotionTarget=main)', () => {
    writePrHelper({
      taskId: 'task-foreign-1',
      workBranch: 'blueprint/task-foreign-1',
      baseBranch: 'automain',
      owner: 'SomeOtherOrg',
      repo: 'SomeOtherRepo',
      testsOnMainBranch: false,
      promotionTarget: 'main',
    });
    const script = capturedScripts['/tmp/workspace/pr'];
    expect(script).toBeDefined();
    // Foreign repo off promotion target: shouldRunBackendMergeGate is false.
    expect(script).not.toContain('# Mandatory test gate: force WITH_TESTS=1 when the diff touches backend/');
  });

  // Assert the generated ./pr script emits the fail-closed-aware create-PR
  // backoff — honoring Retry-After, exponential backoff with a cap, 503 treated
  // as retryable, and the old tight linear loop removed.
  it('emits Retry-After-aware, capped exponential create-PR backoff (503 retryable)', () => {
    writePrHelper({
      taskId: 'task-backoff-1',
      workBranch: 'blueprint/task-backoff-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const s = capturedScripts['/tmp/workspace/pr'];
    expect(s).toBeDefined();
    // Dumps response headers so a server Retry-After can be read.
    expect(s).toContain('-D "$PR_HDR_FILE"');
    expect(s).toContain("grep -i '^Retry-After:'");
    // Exponential backoff (5,10,20,40s), capped at 120s.
    expect(s).toContain('BACKOFF=$((5 * (1 << (attempt - 1))))');
    expect(s).toContain('[ "$BACKOFF" -gt 120 ] && BACKOFF=120');
    // 503 (and any 5xx) plus 429/000/401/403 are retryable; other 4xx are not.
    expect(s).toContain('401|403|429|000|5[0-9][0-9]');
    // The old tight linear backoff (~3-9s) is gone.
    expect(s).not.toContain('BACKOFF=$((attempt * 3))');
  });

  it('emits the zero-effective-diff guard after the commits-ahead check', () => {
    writePrHelper({
      taskId: 'task-static-diff',
      workBranch: 'blueprint/task-static-diff',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });
    const s = capturedScripts['/tmp/workspace/pr'];
    expect(s).toBeDefined();
    // Guards on the three-dot (merge-base) diff so it matches merge semantics,
    // skips when BASE_REF is unresolvable, and routes to the sanctioned no-op exits.
    expect(s).toContain('_EFFECTIVE_DIFF=$(git -c core.quotepath=false diff --name-only "$BASE_REF...HEAD"');
    expect(s).toContain('nets to ZERO effective diff vs base');
    expect(s).toContain('./pr close --superseded-by');
    expect(s).toContain('./done --already-done');
    // Distinguishes a FAILED diff (depth-limited clone, merge-base absent) from a
    // genuinely-empty one: captures the exit status separately, deepens+retries on
    // failure, and treats a persistent failure as inconclusive (skip) — NOT a hard
    // block. The zero-diff hard block only fires when the diff SUCCEEDED but empty.
    expect(s).toContain('_EFFECTIVE_DIFF_STATUS=$?');
    expect(s).toContain('git fetch --deepen=200 origin');
    expect(s).toContain('effective-diff check inconclusive');
    expect(s).toMatch(/if \[ "\$_EFFECTIVE_DIFF_STATUS" -ne 0 \][\s\S]*elif \[ -z "\$_EFFECTIVE_DIFF" \]/);
  });
});
