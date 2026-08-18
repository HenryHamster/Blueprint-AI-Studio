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

import { prepareLifecycleHelperTestRepo } from './lifecycle-helper-test-repo.js';

describe('writeCodeChangeHelpers — ./blocked runtime receipt', () => {
  let capturedScripts: Record<string, string> = {};
  let sequence = 0;

  beforeEach(() => {
    capturedScripts = {};
    vi.mocked(fs.writeFileSync).mockImplementation((...args: unknown[]) => {
      const [path, content] = args as [string, string];
      capturedScripts[path] = content;
    });
  });

  const runBlocked = async (
    receipt: 'exact' | 'wrong-task',
  ): Promise<{ exit: number; payload: Record<string, unknown> | null }> => {
    const realFs = await vi.importActual<typeof import('node:fs')>('node:fs');
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    const realOs = await vi.importActual<typeof import('node:os')>('node:os');
    const realPath = await vi.importActual<typeof import('node:path')>('node:path');
    const taskId = `blocked-receipt-${process.pid}-${sequence++}`;
    const { writeCodeChangeHelpers } = await import('./code-change-helpers.js');
    writeCodeChangeHelpers({
      taskId,
      workBranch: `blueprint/${taskId}`,
      baseBranch: 'main',
      owner: 'test-owner',
      repo: 'test-repo',
      testsOnMainBranch: true,
    });

    const receiptPath = `/tmp/execution_capability_receipt_${taskId}`;
    const tmpPaths = [
      `/tmp/blocked_${taskId}`,
      `/tmp/base_commit_${taskId}`,
      receiptPath,
    ];
    const cleanup = () => {
      try {
        realCp.execFileSync(
          'bash',
          ['-c', 'rm -f -- "$@"', 'blocked-cleanup', ...tmpPaths],
        );
      } catch { /* best effort */ }
    };
    cleanup();
    const receiptTask = receipt === 'exact' ? taskId : 'another-task';
    realCp.execFileSync(
      'bash',
      [
        '-c',
        'printf "%s\\n" "$1" > "$2"',
        'blocked-receipt',
        `backend_verification:runtime_unavailable:v1:${receiptTask}`,
        receiptPath,
      ],
    );

    const dir = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), 'blocked-receipt-'));
    try {
      const scriptPath = realPath.join(dir, 'blocked');
      realFs.writeFileSync(scriptPath, capturedScripts['/tmp/workspace/blocked'], {
        mode: 0o755,
      });
      const bin = realPath.join(dir, 'bin');
      realFs.mkdirSync(bin);
      const payloadFile = realPath.join(dir, 'payload.json');
      realFs.writeFileSync(
        realPath.join(bin, 'curl'),
        `#!/bin/bash\ncat > "${payloadFile}"\nprintf '200'\n`,
        { mode: 0o755 },
      );
      prepareLifecycleHelperTestRepo(dir, taskId);
      const env = {
        ...process.env,
        ORCHESTRATOR_URL: 'http://orchestrator.test',
        AGENT_API_TOKEN: 'tok',
      };
      let exit = 0;
      try {
        // Preserve the host PATH while Node locates bash, then prepend the curl
        // stub in bash's path syntax. cygpath handles Git Bash on Windows.
        realCp.execFileSync(
          'bash',
          [
            '-c',
            'STUB_BIN="$1"; if command -v cygpath >/dev/null 2>&1; then STUB_BIN=$(cygpath -u "$STUB_BIN"); fi; PATH="$STUB_BIN:$PATH"; shift; exec bash "$@"',
            'blocked-test',
            bin,
            scriptPath,
            'postgres missing',
            '--needs-capability',
            'backend verification',
          ],
          { stdio: 'pipe', env },
        );
      } catch (error: unknown) {
        exit = (error as { status?: number }).status ?? -1;
      }
      const payload = realFs.existsSync(payloadFile)
        ? JSON.parse(realFs.readFileSync(payloadFile, 'utf8')) as Record<string, unknown>
        : null;
      return { exit, payload };
    } finally {
      realFs.rmSync(dir, { recursive: true, force: true });
      cleanup();
    }
  };

  it('routes an exact task-bound receipt through the ./fail capability authority', async () => {
    const { exit, payload } = await runBlocked('exact');
    expect(exit).toBe(0);
    expect(payload).toMatchObject({
      status: 'FAILED',
      failure_category: 'agent_self_failed',
      execution_capability: 'backend_verification',
    });
    // Git for Windows' MSYS argv bridge can mojibake the em dash; Linux keeps
    // the exact UTF-8 frame which the backend schema validates.
    expect(String(payload?.logs).replace('â€”', '—')).toBe(
      '[check] required backend verification state=runtime_unavailable — PostgreSQL runtime unavailable',
    );
    expect(payload).not.toHaveProperty('blocked_need');
  });

  it('keeps a wrong-task receipt on ordinary BLOCKED_PRECONDITION routing', async () => {
    const { exit, payload } = await runBlocked('wrong-task');
    expect(exit).toBe(0);
    expect(payload).toMatchObject({
      failure_category: 'BLOCKED_PRECONDITION',
      logs: 'BLOCKER: postgres missing',
      blocked_need: { kind: 'capability', ref: 'backend verification' },
    });
    expect(payload).not.toHaveProperty('execution_capability');
  });
});
