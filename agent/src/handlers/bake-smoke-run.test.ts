import { describe, it, expect, afterAll } from 'vitest';
import { spawnSync } from 'child_process';
import { mkdtempSync, rmSync, writeFileSync, readFileSync } from 'fs';
import { tmpdir } from 'os';
import { fileURLToPath } from 'url';
import path from 'path';

const agentDir = fileURLToPath(new URL('../..', import.meta.url));
const handlersDir = fileURLToPath(new URL('.', import.meta.url));

function smokeScript(name: string): string {
  return path.join(handlersDir, name);
}

function runSmoke(
  scriptName: string,
  extraEnv: Record<string, string> = {},
): ReturnType<typeof spawnSync> {
  return spawnSync(
    'node',
    ['--import', 'tsx', smokeScript(scriptName)],
    {
      cwd: agentDir,
      env: {
        ...process.env,
        ORCHESTRATOR_URL: 'http://127.0.0.1:9',
        AGENT_TOKEN: 'packer-smoke',
        ...extraEnv,
      },
      timeout: 120_000,
      encoding: 'utf-8',
    },
  );
}

function failMsg(
  scriptName: string,
  result: ReturnType<typeof spawnSync>,
): string {
  const combined = String(result.stdout) + String(result.stderr);
  return (
    `${scriptName} exited ${String(result.status)} (signal=${String(result.signal)})\n` +
    `output tail:\n${combined.slice(-500)}`
  );
}

// Shared isolated workspace for subprocesses that reach configureAgentPermissions.
// Without this, those subprocesses write exit-1 git hooks into the real VM workspace.
const smokeTmpDir = mkdtempSync(path.join(tmpdir(), 'bake-smoke-'));
let lifecycleTmpDir: string | null = null;

afterAll(() => {
  try {
    rmSync(smokeTmpDir, { recursive: true, force: true });
  } catch { /* ignore */ }
  if (lifecycleTmpDir) {
    try {
      rmSync(lifecycleTmpDir, { recursive: true, force: true });
    } catch { /* ignore */ }
  }
});

describe('memo bake-smoke scripts execute with PACKER sentinel and exit 0', { timeout: 120_000 }, () => {
  it('memo-bake-smoke.ts exits 0 (dispatcher path)', () => {
    const result = runSmoke('memo-bake-smoke.ts');
    expect(result.status, failMsg('memo-bake-smoke.ts', result)).toBe(0);
  });

  it('memo-bake-smoke-codex.ts exits 0 and emits PACKER_MEMO_CLAUDE_PATH_OK', () => {
    const result = runSmoke('memo-bake-smoke-codex.ts', {
      AGENT_WORKSPACE: smokeTmpDir,
    });
    expect(result.status, failMsg('memo-bake-smoke-codex.ts', result)).toBe(0);
    expect(
      String(result.stdout),
      failMsg('memo-bake-smoke-codex.ts', result),
    ).toContain('PACKER_MEMO_CLAUDE_PATH_OK');
  });

  it('memo-bake-smoke-primary.ts exits 0 and emits PACKER_MEMO_PRIMARY_PATH_OK', () => {
    const result = runSmoke('memo-bake-smoke-primary.ts', {
      AGENT_WORKSPACE: smokeTmpDir,
    });
    expect(result.status, failMsg('memo-bake-smoke-primary.ts', result)).toBe(0);
    expect(
      String(result.stdout),
      failMsg('memo-bake-smoke-primary.ts', result),
    ).toContain('PACKER_MEMO_PRIMARY_PATH_OK');
  });

  it('memo-bake-smoke-reopen.ts exits 0 and emits PACKER_MEMO_REOPEN_RETRY_OK', () => {
    const result = runSmoke('memo-bake-smoke-reopen.ts', {
      AGENT_WORKSPACE: smokeTmpDir,
    });
    expect(result.status, failMsg('memo-bake-smoke-reopen.ts', result)).toBe(0);
    expect(
      String(result.stdout),
      failMsg('memo-bake-smoke-reopen.ts', result),
    ).toContain('PACKER_MEMO_REOPEN_RETRY_OK');
  });

  it('memo-bake-smoke-lifecycle.ts exits 0 and emits PACKER_MEMO_LIFECYCLE_OK', () => {
    lifecycleTmpDir = mkdtempSync(path.join(tmpdir(), 'bake-smoke-lifecycle-'));
    const result = runSmoke('memo-bake-smoke-lifecycle.ts', {
      BLUEPRINT_SMOKE_SKIP_VALIDATION: '1',
      AGENT_WORKSPACE: lifecycleTmpDir,
    });
    expect(result.status, failMsg('memo-bake-smoke-lifecycle.ts', result)).toBe(0);
    expect(
      String(result.stdout),
      failMsg('memo-bake-smoke-lifecycle.ts', result),
    ).toContain('PACKER_MEMO_LIFECYCLE_OK');
  });

  it('memo-bake-smoke-lifecycle.ts does not write the host AGENT_TOKEN_FILE path', () => {
    // Use separate dirs: the agent deletes AGENT_WORKSPACE during workspace prep,
    // so the host token file must live outside the workspace directory.
    const workspaceTmpDir = mkdtempSync(path.join(tmpdir(), 'bake-smoke-lifecycle-ws-'));
    const hostTokenDir = mkdtempSync(path.join(tmpdir(), 'bake-smoke-token-isolation-'));
    try {
      // Simulate the host session having a live JWT at a known path.
      const hostTokenFile = path.join(hostTokenDir, 'host-agent-token.jwt');
      const hostSentinel = 'host-session-jwt-sentinel-must-not-be-overwritten';
      writeFileSync(hostTokenFile, hostSentinel, { encoding: 'utf-8' });

      // Pass the host token path via AGENT_TOKEN_FILE. The smoke lifecycle
      // script must redirect child writes to a sandbox path, not this one.
      const result = runSmoke('memo-bake-smoke-lifecycle.ts', {
        BLUEPRINT_SMOKE_SKIP_VALIDATION: '1',
        AGENT_WORKSPACE: workspaceTmpDir,
        AGENT_TOKEN_FILE: hostTokenFile,
      });
      expect(result.status, failMsg('memo-bake-smoke-lifecycle.ts (token isolation)', result)).toBe(0);

      // The host token file must still contain the original sentinel — not the
      // smoke fixture token ('lifecycle-smoke-token') written by the child agent.
      const afterContent = readFileSync(hostTokenFile, { encoding: 'utf-8' });
      expect(afterContent).toBe(hostSentinel);
    } finally {
      try { rmSync(workspaceTmpDir, { recursive: true, force: true }); } catch { /* ignore */ }
      try { rmSync(hostTokenDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  });
});
