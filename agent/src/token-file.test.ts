import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  AGENT_TOKEN_FILE_ENV,
  agentTokenFilePath,
  ensureAgentTokenFileEnv,
  writeAgentTokenFile,
  agentTokenShellFn,
} from './token-file.js';

// Isolate every test from the real home dir: each run points AGENT_TOKEN_FILE at
// a unique temp path and restores the prior env afterward.
const tmpRoot = path.join(os.tmpdir(), `token-file-test-${process.pid}`);
let savedEnv: string | undefined;

beforeEach(() => {
  savedEnv = process.env.AGENT_TOKEN_FILE;
  delete process.env.AGENT_TOKEN_FILE;
  fs.mkdirSync(tmpRoot, { recursive: true });
});

afterEach(() => {
  if (savedEnv === undefined) delete process.env.AGENT_TOKEN_FILE;
  else process.env.AGENT_TOKEN_FILE = savedEnv;
  try { fs.rmSync(tmpRoot, { recursive: true, force: true }); } catch { /* ignore */ }
});

describe('agentTokenFilePath', () => {
  it('honors an explicit AGENT_TOKEN_FILE override', () => {
    process.env[AGENT_TOKEN_FILE_ENV] = '/custom/run/agent.jwt';
    expect(agentTokenFilePath()).toBe('/custom/run/agent.jwt');
  });

  it('defaults to a private .blueprint path OUTSIDE the cloned workspace', () => {
    const p = agentTokenFilePath();
    expect(p).toContain('.blueprint');
    expect(p.endsWith('agent-token.jwt')).toBe(true);
    // Must never live under the agent workspace (which is cloned/committed).
    expect(p.includes(`${path.sep}workspace${path.sep}`)).toBe(false);
  });
});

describe('ensureAgentTokenFileEnv', () => {
  it('publishes the resolved path into the env when unset', () => {
    expect(process.env[AGENT_TOKEN_FILE_ENV]).toBeUndefined();
    const p = ensureAgentTokenFileEnv();
    expect(process.env[AGENT_TOKEN_FILE_ENV]).toBe(p);
    expect(p).toContain('.blueprint');
  });

  it('keeps an already-configured path (idempotent)', () => {
    process.env[AGENT_TOKEN_FILE_ENV] = '/preset/path.jwt';
    expect(ensureAgentTokenFileEnv()).toBe('/preset/path.jwt');
    expect(process.env[AGENT_TOKEN_FILE_ENV]).toBe('/preset/path.jwt');
  });
});

describe('writeAgentTokenFile', () => {
  it('no-ops (returns false) when AGENT_TOKEN_FILE is not configured', () => {
    // Env deleted in beforeEach — a stray import must never write to the real home.
    expect(writeAgentTokenFile('jwt-abc')).toBe(false);
  });

  it('no-ops (returns false) for an empty token', () => {
    process.env[AGENT_TOKEN_FILE_ENV] = path.join(tmpRoot, 'a', 'agent.jwt');
    expect(writeAgentTokenFile('')).toBe(false);
    expect(fs.existsSync(path.join(tmpRoot, 'a', 'agent.jwt'))).toBe(false);
  });

  it('writes the token to the configured file, creating nested dirs', () => {
    const target = path.join(tmpRoot, 'nested', 'dir', 'agent.jwt');
    process.env[AGENT_TOKEN_FILE_ENV] = target;
    expect(writeAgentTokenFile('jwt-live-1')).toBe(true);
    expect(fs.readFileSync(target, 'utf-8')).toBe('jwt-live-1');
    // No leftover temp file from the atomic write.
    expect(fs.existsSync(`${target}.tmp`)).toBe(false);
  });

  it('overwrites the file on refresh so helpers read the ROTATED token', () => {
    const target = path.join(tmpRoot, 'agent.jwt');
    process.env[AGENT_TOKEN_FILE_ENV] = target;
    writeAgentTokenFile('jwt-old');
    expect(fs.readFileSync(target, 'utf-8')).toBe('jwt-old');
    // Simulate the ~45min proactive refresh calling setToken → writeAgentTokenFile.
    writeAgentTokenFile('jwt-new');
    expect(fs.readFileSync(target, 'utf-8')).toBe('jwt-new');
  });

  it('writes the token file with owner-only 0600 perms (POSIX)', () => {
    const target = path.join(tmpRoot, 'perm.jwt');
    process.env[AGENT_TOKEN_FILE_ENV] = target;
    writeAgentTokenFile('jwt-secret');
    if (process.platform !== 'win32') {
      const mode = fs.statSync(target).mode & 0o777;
      expect(mode).toBe(0o600);
    } else {
      // POSIX perms are not modeled on Windows — just assert the file exists.
      expect(fs.existsSync(target)).toBe(true);
    }
  });
});

describe('agentTokenShellFn', () => {
  it('emits a _load_agent_token function that prefers the file and falls back to env', () => {
    const fn = agentTokenShellFn();
    expect(fn).toContain('_load_agent_token()');
    // Reads the live file fresh...
    expect(fn).toContain('[ -s "${AGENT_TOKEN_FILE}" ]');
    expect(fn).toContain('TOKEN=$(cat "${AGENT_TOKEN_FILE}" 2>/dev/null)');
    // ...and falls back to the env snapshot for one-shot agents.
    expect(fn).toContain('TOKEN="${AGENT_API_TOKEN:-}"');
    // Fails loud only when BOTH sources are empty.
    expect(fn).toContain('no agent token available');
    // Must never echo the token value.
    expect(fn).not.toContain('echo "$TOKEN"');
  });
});
