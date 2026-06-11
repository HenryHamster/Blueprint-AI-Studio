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
  ): Promise<{ status: number; stdout: string; checkArgs?: string }> => {
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
        return { status: 0, stdout, checkArgs };
      } catch (err: unknown) {
        const e = err as { status?: number; stdout?: string };
        return { status: e.status ?? -1, stdout: e.stdout ?? '' };
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
  *"status --porcelain"*) exit 0 ;;
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
  *"status --porcelain"*) exit 0 ;;
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
    expect(result.stdout).toContain('mergeable=false, mergeable_state=dirty');
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
  });
});
