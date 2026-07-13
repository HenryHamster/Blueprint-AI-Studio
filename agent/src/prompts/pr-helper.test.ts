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
            WORKSPACE: dir,
            PATH: `${realPath.join(dir, 'bin')}:${process.env.PATH ?? ''}`,
            ORCHESTRATOR_URL: 'https://orchestrator.example',
            AGENT_API_TOKEN: 'token-123',
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
  *"push --force-with-lease origin blueprint/task-workflow-2"*)
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
    const script = writeScript({
      taskId: 'task-retry-1',
      workBranch: 'blueprint/task-retry-1',
      baseBranch: 'main',
      owner: 'acme',
      repo: 'repo',
      testsOnMainBranch: false,
    });

    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    // Cross-process handoff files live at absolute /tmp paths (survive the temp
    // dir cleanup). Start clean so the attempt counter is authoritative.
    realFs.rmSync('/tmp/pr_attempts_task-retry-1', { force: true });
    realFs.rmSync('/tmp/pr_url_task-retry-1', { force: true });

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
  *"push --force-with-lease origin blueprint/task-retry-1"*) exit 0 ;;
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
  *"/api/agent/github/token?task_id=task-retry-1"*) printf '{"data":{"token":"token-abc"}}'; exit 0 ;;
  *"/api/agent/github/pull-requests"*)
    N=$(cat /tmp/pr_attempts_task-retry-1 2>/dev/null || echo 0)
    N=$((N + 1)); echo "$N" > /tmp/pr_attempts_task-retry-1
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
    expect(realFs.readFileSync('/tmp/pr_attempts_task-retry-1', 'utf8').trim()).toBe('2');
    expect(realFs.readFileSync('/tmp/pr_url_task-retry-1', 'utf8').trim()).toBe('https://github.com/acme/repo/pull/7');
    realFs.rmSync('/tmp/pr_attempts_task-retry-1', { force: true });
    realFs.rmSync('/tmp/pr_url_task-retry-1', { force: true });
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
    expect(parsed.body).toBe(specialBody);

    // (b) Assert the html_url was extracted (via _json_field) and PR_URL persisted.
    const prUrl = realFs.readFileSync('/tmp/pr_url_task-nojq-1', 'utf8').trim();
    expect(prUrl).toBe('https://github.com/acme/repo/pull/99');

    // Cleanup.
    realFs.rmSync('/tmp/pr_body_task-nojq-1', { force: true });
    realFs.rmSync('/tmp/pr_url_task-nojq-1', { force: true });
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
});

// Static content checks (no bash execution — these run on every platform,
// including Windows where the POSIX integration suite above is skipped).
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
    // The marker is read AFTER the gate passes, inside the WITH_TESTS branch —
    // a red gate still blocks; the note only annotates a green one.
    expect(script).toMatch(/if ! \.\/check test; then[\s\S]*exit 1[\s\S]*\n {2}fi\n[\s\S]*check_gate_env_task-static-1\.txt/);
    expect(script).toContain('if [ -s /tmp/check_gate_env_task-static-1.txt ]; then');
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
  });
});
