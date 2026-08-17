import { beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs';

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

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'pr-helper-'));
    try {
      setup(dir, realFs);
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
    const taskId = `task-retry-${process.pid}`;
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
    expect(parsed.body).toContain(specialBody);
    expect(parsed.body).toContain('- Status: passed');

    // (b) Assert the html_url was extracted (via _json_field) and PR_URL persisted.
    const prUrl = realFs.readFileSync('/tmp/pr_url_task-nojq-1', 'utf8').trim();
    expect(prUrl).toBe('https://github.com/acme/repo/pull/99');

    // Cleanup.
    realFs.rmSync('/tmp/pr_body_task-nojq-1', { force: true });
    realFs.rmSync('/tmp/pr_url_task-nojq-1', { force: true });
  });

  const assertSuppressedBaselineReceipt = async (
    receiptShape: 'writer-shaped nested' | 'legacy flat',
  ): Promise<void> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const taskId = receiptShape === 'writer-shaped nested'
      ? 'task-suppressed-baseline-nested-1'
      : 'task-suppressed-baseline-legacy-1';
    const receiptPath = `/tmp/project_verification_receipt_${taskId}.json`;
    const bodyPath = `/tmp/pr_body_${taskId}`;
    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(bodyPath, { force: true });

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

    const result = await runScript(script, ['open', '--title', 'title', '--body', 'body'], (dir, rfs) => {
      const bin = `${dir}/bin`;
      rfs.mkdirSync(bin, { recursive: true });
      rfs.writeFileSync(
        receiptPath,
        JSON.stringify(receipt),
      );
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
    expect(body.body).toContain('- Status: passed');
    expect(body.body).toContain('### Suppressed baseline failures');
    expect(body.body).toContain('`backend/testing/test_a.py::test_older::error`: RuntimeError: baseline-only issue');
    expect(body.body).toContain('`backend/testing/test_z.py::test_inherited::failure`: AssertionError: inherited failure');
    expect(body.body.indexOf('test_a.py')).toBeLessThan(body.body.indexOf('test_z.py'));
    expect(body.body).toContain('<!-- /blueprint:local-gate -->');

    realFs.rmSync(receiptPath, { force: true });
    realFs.rmSync(bodyPath, { force: true });
    realFs.rmSync(`/tmp/pr_url_${taskId}`, { force: true });
  };

  it.each(['writer-shaped nested', 'legacy flat'] as const)(
    'surfaces suppressed baseline failures from a %s receipt in the local-gate PR section',
    assertSuppressedBaselineReceipt,
  );

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

  it('emits the mandatory test gate for self-repo automain task PR (baseBranch=automain, promotionTarget=main)', () => {
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
    expect(script).toContain('# Mandatory test gate: force WITH_TESTS=1 when the diff touches backend/');
    expect(script).toContain('backend/ touched on LTS branch — forcing WITH_TESTS=1 (mandatory test gate)');
    expect(script).toContain('WITH_TESTS=1');
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
