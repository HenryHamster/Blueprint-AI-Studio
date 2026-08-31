import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  AGENT_TOKEN_FILE_ENV,
  agentTokenFilePath,
  ensureAgentTokenFileEnv,
  registerAgentTokenMirror,
  writeAgentTokenFile,
  agentTokenShellFn,
  GITHUB_TOKEN_FILE_ENV,
  githubTokenFilePath,
  ensureGithubTokenFileEnv,
  writeGithubTokenFile,
  githubTokenShellFn,
  jsonStrShellFn,
  jsonFieldShellFn,
} from './token-file.js';

// Isolate every test from the real home dir: each run points token files at
// unique temp paths and restores the prior env afterward.
const tmpRoot = path.join(os.tmpdir(), `token-file-test-${process.pid}`);
let savedEnv: string | undefined;
let savedGhEnv: string | undefined;

beforeEach(() => {
  savedEnv = process.env.AGENT_TOKEN_FILE;
  savedGhEnv = process.env.GITHUB_TOKEN_FILE;
  delete process.env.AGENT_TOKEN_FILE;
  delete process.env.GITHUB_TOKEN_FILE;
  fs.mkdirSync(tmpRoot, { recursive: true });
});

afterEach(() => {
  if (savedEnv === undefined) delete process.env.AGENT_TOKEN_FILE;
  else process.env.AGENT_TOKEN_FILE = savedEnv;
  if (savedGhEnv === undefined) delete process.env.GITHUB_TOKEN_FILE;
  else process.env.GITHUB_TOKEN_FILE = savedGhEnv;
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

  it('refreshes every registered Resident mirror until it is fenced', () => {
    const configured = path.join(tmpRoot, 'configured', 'agent.jwt');
    const residentOne = path.join(tmpRoot, 'resident-one', 'agent.jwt');
    const residentTwo = path.join(tmpRoot, 'resident-two', 'agent.jwt');
    process.env[AGENT_TOKEN_FILE_ENV] = configured;
    const unregisterOne = registerAgentTokenMirror(residentOne);
    const unregisterTwo = registerAgentTokenMirror(residentTwo);
    try {
      expect(writeAgentTokenFile('jwt-first')).toBe(true);
      expect(fs.readFileSync(configured, 'utf-8')).toBe('jwt-first');
      expect(fs.readFileSync(residentOne, 'utf-8')).toBe('jwt-first');
      expect(fs.readFileSync(residentTwo, 'utf-8')).toBe('jwt-first');

      unregisterOne();
      expect(writeAgentTokenFile('jwt-refreshed')).toBe(true);
      expect(fs.readFileSync(configured, 'utf-8')).toBe('jwt-refreshed');
      expect(fs.readFileSync(residentOne, 'utf-8')).toBe('jwt-first');
      expect(fs.readFileSync(residentTwo, 'utf-8')).toBe('jwt-refreshed');
    } finally {
      unregisterOne();
      unregisterTwo();
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
    // Rejects placeholder/non-JWT file contents so the env fallback can recover.
    expect(fn).toContain('_agent_token_looks_like_jwt()');
    expect(fn).toContain('! _agent_token_looks_like_jwt "$TOKEN"');
    expect(fn).toContain('ignoring invalid AGENT_TOKEN_FILE contents');
    // ...and falls back to the env snapshot for one-shot agents.
    expect(fn).toContain('TOKEN="${AGENT_API_TOKEN:-}"');
    // Fails loud only when BOTH sources are empty.
    expect(fn).toContain('no agent token available');
    // Must never echo the token value.
    expect(fn).not.toContain('echo "$TOKEN"');
  });
});

describe('jsonStrShellFn', () => {
  it('emits a jq-free JSON string encoder with a fail-loud fallback', () => {
    const fn = jsonStrShellFn();
    expect(fn).toContain('_json_str()');
    expect(fn).toContain('command -v python3');
    expect(fn).toContain('json.dumps');
    expect(fn).toContain('command -v node');
    expect(fn).toContain('JSON.stringify');
    expect(fn).toContain('no JSON string encoder available');
    expect(fn).toContain('_json_str "" >/dev/null || exit 1');
    expect(fn).not.toContain('jq -');
  });
});

describe('jsonFieldShellFn', () => {
  it('emits a jq-free JSON field extractor with dot-path walking and error handling', () => {
    const fn = jsonFieldShellFn();
    expect(fn).toContain('_json_field()');
    expect(fn).toContain('import json, sys');
    expect(fn).toContain('json.loads(sys.stdin.read())');
    expect(fn).toContain('parts = path.split(".")');
    expect(fn).toContain('isinstance(data, dict)');
    expect(fn).toContain('data = data.get(p)');
    expect(fn).toContain('except Exception');
    // The doc comment references jq for context ("matches jq -r '… // empty'"),
    // but the function itself has no jq dependency — it uses python3 exclusively.
    const bodyAfterDocComment = fn.slice(fn.indexOf('_json_field() {'));
    expect(bodyAfterDocComment).not.toContain('jq');
  });

  it('emits a function that accepts an optional default value argument', () => {
    const fn = jsonFieldShellFn();
    // The default argument is consumed via sys.argv[2] when present
    expect(fn).toContain('sys.argv[2] if len(sys.argv) > 2 else ""');
  });
});

// ── GitHub token file ─────────────────────────────────────────────────────────

describe('githubTokenFilePath', () => {
  it('honors an explicit GITHUB_TOKEN_FILE override', () => {
    process.env[GITHUB_TOKEN_FILE_ENV] = '/custom/run/github-token.txt';
    expect(githubTokenFilePath()).toBe('/custom/run/github-token.txt');
  });

  it('defaults to a private .blueprint path OUTSIDE the cloned workspace', () => {
    const p = githubTokenFilePath();
    expect(p).toContain('.blueprint');
    expect(p.endsWith('github-token.txt')).toBe(true);
    expect(p.includes(`${path.sep}workspace${path.sep}`)).toBe(false);
  });
});

describe('ensureGithubTokenFileEnv', () => {
  it('publishes the resolved path into the env when unset', () => {
    expect(process.env[GITHUB_TOKEN_FILE_ENV]).toBeUndefined();
    const p = ensureGithubTokenFileEnv();
    expect(process.env[GITHUB_TOKEN_FILE_ENV]).toBe(p);
    expect(p).toContain('.blueprint');
  });

  it('keeps an already-configured path (idempotent)', () => {
    process.env[GITHUB_TOKEN_FILE_ENV] = '/preset/github.txt';
    expect(ensureGithubTokenFileEnv()).toBe('/preset/github.txt');
    expect(process.env[GITHUB_TOKEN_FILE_ENV]).toBe('/preset/github.txt');
  });
});

describe('writeGithubTokenFile', () => {
  it('no-ops (returns false) when GITHUB_TOKEN_FILE is not configured', () => {
    expect(writeGithubTokenFile('ghs_abc')).toBe(false);
  });

  it('no-ops (returns false) for an empty token', () => {
    process.env[GITHUB_TOKEN_FILE_ENV] = path.join(tmpRoot, 'a', 'github.txt');
    expect(writeGithubTokenFile('')).toBe(false);
    expect(fs.existsSync(path.join(tmpRoot, 'a', 'github.txt'))).toBe(false);
  });

  it('writes the token to the configured file, creating nested dirs', () => {
    const target = path.join(tmpRoot, 'nested', 'dir', 'github.txt');
    process.env[GITHUB_TOKEN_FILE_ENV] = target;
    expect(writeGithubTokenFile('ghs_live-1')).toBe(true);
    expect(fs.readFileSync(target, 'utf-8')).toBe('ghs_live-1');
    expect(fs.existsSync(`${target}.tmp`)).toBe(false);
  });

  it('overwrites the file on refresh so helpers read the ROTATED token', () => {
    const target = path.join(tmpRoot, 'github.txt');
    process.env[GITHUB_TOKEN_FILE_ENV] = target;
    writeGithubTokenFile('ghs_old');
    expect(fs.readFileSync(target, 'utf-8')).toBe('ghs_old');
    writeGithubTokenFile('ghs_new');
    expect(fs.readFileSync(target, 'utf-8')).toBe('ghs_new');
  });

  it('writes the token file with owner-only 0600 perms (POSIX)', () => {
    const target = path.join(tmpRoot, 'perm-gh.txt');
    process.env[GITHUB_TOKEN_FILE_ENV] = target;
    writeGithubTokenFile('ghs_secret');
    if (process.platform !== 'win32') {
      const mode = fs.statSync(target).mode & 0o777;
      expect(mode).toBe(0o600);
    } else {
      expect(fs.existsSync(target)).toBe(true);
    }
  });
});

describe('githubTokenShellFn', () => {
  it('emits _load_github_token that prefers the file and falls back to API', () => {
    const fn = githubTokenShellFn();
    expect(fn).toContain('_load_github_token()');
    // Reads from the live token file first.
    expect(fn).toContain('[ -s "${GITHUB_TOKEN_FILE}" ]');
    expect(fn).toContain('GH_TOKEN=$(cat "${GITHUB_TOKEN_FILE}" 2>/dev/null)');
    // Falls back to the backend API.
    expect(fn).toContain('api/agent/github/token');
    // Fails loud when neither source yields a token.
    expect(fn).toContain('no GitHub token available');
    // Must never echo the token value.
    expect(fn).not.toContain('echo "$GH_TOKEN"');
  });

  it('emits _set_github_remote that rewrites the origin URL with the token', () => {
    const fn = githubTokenShellFn();
    expect(fn).toContain('_set_github_remote()');
    expect(fn).toContain('git remote set-url origin');
    expect(fn).toContain('x-access-token:${GH_TOKEN}@github.com');
  });

  it('suppresses _set_github_remote remote-URL rewrite for read-only seats (call site)', () => {
    // For read-only task types the caller (pr-helper generator) must NOT emit the
    // _set_github_remote call. The function itself is still defined in the shell
    // output (it's part of the canonical helper block) — only the invocation is
    // omitted. Verify the function definition is present in the emitted block.
    const fn = githubTokenShellFn();
    expect(fn).toContain('_set_github_remote()');
    // The READ-ONLY suppression is enforced at generation time in pr-helper.ts
    // via cfg.isReadOnly — not inside githubTokenShellFn itself.
    // This test documents the contract: the shell fn block is always emitted;
    // the caller decides whether to call _set_github_remote.
  });
});
