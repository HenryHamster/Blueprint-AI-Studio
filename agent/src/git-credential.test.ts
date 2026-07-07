import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('child_process', () => ({
  execFileSync: vi.fn(),
}));

import { execFileSync } from 'child_process';
import {
  gitTokenFilePath,
  gitCredentialHelperPath,
  writeGitTokenFile,
  setupGitCredentialHelper,
  ensureGitCredentialHelperScript,
  GH_READ_TOKEN_FILE_ENV,
  GH_CREDENTIAL_HELPER_PATH_ENV,
  GIT_TOKEN_REFRESH_MS,
} from './git-credential.js';

describe('gitTokenFilePath', () => {
  it('returns a path under ~/.blueprint/', () => {
    const p = gitTokenFilePath();
    expect(p).toContain('.blueprint');
    expect(p).toContain('gh-read-token');
    expect(path.isAbsolute(p)).toBe(true);
  });
});

describe('gitCredentialHelperPath', () => {
  it('returns a path under ~/.blueprint/', () => {
    const p = gitCredentialHelperPath();
    expect(p).toContain('.blueprint');
    expect(p).toContain('git-credential-helper');
    expect(path.isAbsolute(p)).toBe(true);
  });
});

describe('GIT_TOKEN_REFRESH_MS', () => {
  it('is 8 minutes (safe margin given backend serves tokens with >=10 min remaining life)', () => {
    expect(GIT_TOKEN_REFRESH_MS).toBe(8 * 60 * 1000);
  });
});

describe('writeGitTokenFile', () => {
  let tmpDir: string;
  let tokenFilePath: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'git-cred-test-'));
    tokenFilePath = path.join(tmpDir, 'gh-read-token');
    vi.stubEnv(GH_READ_TOKEN_FILE_ENV, tokenFilePath);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('returns false for empty token', () => {
    expect(writeGitTokenFile('')).toBe(false);
  });

  it('writes the token to the env-configured path and returns true', () => {
    const result = writeGitTokenFile('ghs_test_token_abc123');
    expect(result).toBe(true);
    expect(gitTokenFilePath()).toBe(tokenFilePath);
    expect(fs.readFileSync(tokenFilePath, 'utf-8')).toBe('ghs_test_token_abc123');
  });
});

describe('setupGitCredentialHelper', () => {
  let tmpDir: string;
  let helperFilePath: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'git-cred-setup-test-'));
    helperFilePath = path.join(tmpDir, 'git-credential-helper');
    vi.stubEnv(GH_CREDENTIAL_HELPER_PATH_ENV, helperFilePath);
    vi.stubEnv(GH_READ_TOKEN_FILE_ENV, path.join(tmpDir, 'gh-read-token'));
    vi.mocked(execFileSync).mockReset();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('writes the credential helper script as a bash script', () => {
    setupGitCredentialHelper('owner', 'repo', tmpDir);
    const helperPath = gitCredentialHelperPath();
    expect(helperPath).toBe(helperFilePath);
    expect(fs.existsSync(helperPath)).toBe(true);
    const content = fs.readFileSync(helperPath, 'utf-8');
    expect(content).toContain('#!/bin/bash');
    expect(content).toContain('protocol=https');
    expect(content).toContain('host=github.com');
    expect(content).toContain('username=x-access-token');
    expect(content).toContain('password=');
  });

  it('calls git config to set credential helper', () => {
    setupGitCredentialHelper('owner', 'repo', tmpDir);
    const calls = vi.mocked(execFileSync).mock.calls;
    const configCall = calls.find(
      (c) => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('credential.https://github.com.helper'),
    );
    expect(configCall).toBeDefined();
  });

  it('calls git remote set-url to remove embedded credentials', () => {
    setupGitCredentialHelper('myowner', 'myrepo', tmpDir);
    const calls = vi.mocked(execFileSync).mock.calls;
    const remoteCall = calls.find(
      (c) => c[0] === 'git' && Array.isArray(c[1]) && (c[1] as string[]).includes('set-url'),
    );
    expect(remoteCall).toBeDefined();
    const args = remoteCall?.[1] as string[] | undefined;
    expect(args).toBeDefined();
    const newUrl = args?.[args.length - 1] ?? '';
    // The new remote URL must be plain HTTPS (no embedded token)
    expect(newUrl).toBe('https://github.com/myowner/myrepo.git');
    expect(newUrl).not.toContain('x-access-token');
  });

  it('sets GH_READ_TOKEN_FILE env var when unset', () => {
    vi.stubEnv(GH_READ_TOKEN_FILE_ENV, '');
    setupGitCredentialHelper('owner', 'repo', tmpDir);
    expect(process.env[GH_READ_TOKEN_FILE_ENV]).toBeTruthy();
    expect(process.env[GH_READ_TOKEN_FILE_ENV]).toContain('gh-read-token');
  });

  it('does not overwrite GH_READ_TOKEN_FILE if already set', () => {
    const existing = '/custom/path/token';
    vi.stubEnv(GH_READ_TOKEN_FILE_ENV, existing);
    setupGitCredentialHelper('owner', 'repo', tmpDir);
    expect(process.env[GH_READ_TOKEN_FILE_ENV]).toBe(existing);
  });
});

describe('ensureGitCredentialHelperScript — generated bash validity', () => {
  // Register the credential-helper generator (a multi-line bash script emitted
  // from a TS string array with backslash-escape sequences) in the same `bash -n`
  // syntax harness that guards the other generated shell in this repo, rather
  // than only asserting substrings — so an escaping regression is caught as a
  // syntax error at test time.
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'git-cred-bashn-'));
    vi.stubEnv(GH_CREDENTIAL_HELPER_PATH_ENV, path.join(tmpDir, 'git-credential-helper'));
    vi.stubEnv(GH_READ_TOKEN_FILE_ENV, path.join(tmpDir, 'gh-read-token'));
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('emits a helper script that passes `bash -n` syntax check', async () => {
    const helperPath = ensureGitCredentialHelperScript();
    expect(fs.existsSync(helperPath)).toBe(true);
    // child_process.execFileSync is mocked at module scope; use the real module
    // to actually shell out to bash for the syntax check.
    const realCp = await vi.importActual<typeof import('node:child_process')>('node:child_process');
    expect(() => realCp.execFileSync('bash', ['-n', helperPath], { stdio: 'pipe' })).not.toThrow();
  });
});
