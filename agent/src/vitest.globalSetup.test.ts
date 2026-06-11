import { execSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { captureGitState, diffAndRestore } from './vitest.globalSetup.js';

function makeTempGitRepo(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'guard-test-'));
  execSync('git init', { cwd: dir, stdio: 'ignore' });
  execSync('git config user.email "test@test.com"', { cwd: dir, stdio: 'ignore' });
  execSync('git config user.name "Test"', { cwd: dir, stdio: 'ignore' });
  execSync('git remote add origin https://example.com/repo.git', { cwd: dir, stdio: 'ignore' });
  return dir;
}

describe('captureGitState', () => {
  let repoDir: string;

  beforeEach(() => {
    repoDir = makeTempGitRepo();
  });

  afterEach(() => {
    fs.rmSync(repoDir, { recursive: true, force: true });
  });

  it('returns null for a non-git directory', () => {
    const nonRepo = fs.mkdtempSync(path.join(os.tmpdir(), 'non-repo-'));
    try {
      const state = captureGitState(nonRepo);
      expect(state).toBeNull();
    } finally {
      fs.rmSync(nonRepo, { recursive: true, force: true });
    }
  });

  it('captures git config content', () => {
    const state = captureGitState(repoDir);
    expect(state).not.toBeNull();
    if (!state) return;
    expect(state.gitConfigContent).toContain('[remote "origin"]');
  });

  it('captures origin URL', () => {
    const state = captureGitState(repoDir);
    expect(state).not.toBeNull();
    if (!state) return;
    expect(state.originUrl).toBe('https://example.com/repo.git');
  });

  it('enumerates pre-existing hook files', () => {
    const hooksDir = path.join(repoDir, '.git', 'hooks');
    fs.writeFileSync(path.join(hooksDir, 'pre-commit'), '#!/bin/sh\nexit 0', { mode: 0o755 });
    fs.writeFileSync(path.join(hooksDir, 'pre-push'), '#!/bin/sh\nexit 0', { mode: 0o755 });

    const state = captureGitState(repoDir);
    expect(state).not.toBeNull();
    if (!state) return;
    expect(state.hooks['pre-commit']).toContain('exit 0');
    expect(state.hooks['pre-push']).toContain('exit 0');
  });

  it('captures worktree status as empty string for a clean repo', () => {
    const state = captureGitState(repoDir);
    expect(state).not.toBeNull();
    if (!state) return;
    expect(state.worktreeStatus).toBe('');
  });

  it('captures worktree status including untracked files', () => {
    fs.writeFileSync(path.join(repoDir, 'stray.txt'), 'hello');
    const state = captureGitState(repoDir);
    expect(state).not.toBeNull();
    if (!state) return;
    expect(state.worktreeStatus).toContain('stray.txt');
  });
});

describe('diffAndRestore', () => {
  let repoDir: string;

  beforeEach(() => {
    repoDir = makeTempGitRepo();
  });

  afterEach(() => {
    fs.rmSync(repoDir, { recursive: true, force: true });
  });

  it('returns empty array for identical states (clean pass-through)', () => {
    const state = captureGitState(repoDir);
    expect(state).not.toBeNull();
    if (!state) return;
    const mutations = diffAndRestore(state, state, repoDir);
    expect(mutations).toHaveLength(0);
  });

  it('detects .git/config mutation', () => {
    const before = captureGitState(repoDir);
    expect(before).not.toBeNull();
    if (!before) return;
    const gitConfigPath = path.join(repoDir, '.git', 'config');
    fs.appendFileSync(gitConfigPath, '\n[core]\n\thooksPath = /tmp/evil\n');
    const after = captureGitState(repoDir);
    expect(after).not.toBeNull();
    if (!after) return;

    const mutations = diffAndRestore(before, after, repoDir);
    expect(mutations.some(m => m.includes('.git/config'))).toBe(true);
  });

  it('restores .git/config after mutation', () => {
    const before = captureGitState(repoDir);
    expect(before).not.toBeNull();
    if (!before) return;
    const gitConfigPath = path.join(repoDir, '.git', 'config');
    const originalContent = fs.readFileSync(gitConfigPath, 'utf-8');
    fs.appendFileSync(gitConfigPath, '\n[evil]\n\tkey = value\n');
    const after = captureGitState(repoDir);
    expect(after).not.toBeNull();
    if (!after) return;

    diffAndRestore(before, after, repoDir);

    const restored = fs.readFileSync(gitConfigPath, 'utf-8');
    expect(restored).toBe(originalContent);
  });

  it('detects creation of a hook not present before', () => {
    const before = captureGitState(repoDir);
    expect(before).not.toBeNull();
    if (!before) return;
    const hookPath = path.join(repoDir, '.git', 'hooks', 'commit-msg');
    fs.writeFileSync(hookPath, '#!/bin/sh\nexit 1', { mode: 0o755 });
    const after = captureGitState(repoDir);
    expect(after).not.toBeNull();
    if (!after) return;

    const mutations = diffAndRestore(before, after, repoDir);
    expect(mutations.some(m => m.includes('commit-msg') && m.includes('CREATED'))).toBe(true);
  });

  it('restores (removes) a hook that was created by the suite', () => {
    const before = captureGitState(repoDir);
    expect(before).not.toBeNull();
    if (!before) return;
    const hookPath = path.join(repoDir, '.git', 'hooks', 'commit-msg');
    fs.writeFileSync(hookPath, '#!/bin/sh\nexit 1', { mode: 0o755 });
    const after = captureGitState(repoDir);
    expect(after).not.toBeNull();
    if (!after) return;

    diffAndRestore(before, after, repoDir);

    expect(fs.existsSync(hookPath)).toBe(false);
  });

  it('tolerates pre-existing hooks (no mutation detected when unchanged)', () => {
    const hooksDir = path.join(repoDir, '.git', 'hooks');
    fs.writeFileSync(path.join(hooksDir, 'pre-commit'), '#!/bin/sh\nexit 1', { mode: 0o755 });
    fs.writeFileSync(path.join(hooksDir, 'pre-push'), '#!/bin/sh\nexit 1', { mode: 0o755 });

    const before = captureGitState(repoDir);
    expect(before).not.toBeNull();
    if (!before) return;
    const after = captureGitState(repoDir);
    expect(after).not.toBeNull();
    if (!after) return;

    const mutations = diffAndRestore(before, after, repoDir);
    expect(mutations).toHaveLength(0);
  });

  it('detects worktree stray file creation and removes it', () => {
    const before = captureGitState(repoDir);
    expect(before).not.toBeNull();
    if (!before) return;
    const strayPath = path.join(repoDir, 'check.args');
    fs.writeFileSync(strayPath, 'some args');
    const after = captureGitState(repoDir);
    expect(after).not.toBeNull();
    if (!after) return;

    const mutations = diffAndRestore(before, after, repoDir);
    expect(mutations.some(m => m.includes('stray files') || m.includes('check.args'))).toBe(true);
    expect(fs.existsSync(strayPath)).toBe(false);
  });

  it('detects origin URL change', () => {
    const before = captureGitState(repoDir);
    expect(before).not.toBeNull();
    if (!before) return;
    execSync('git remote set-url origin https://evil.com/repo.git', {
      cwd: repoDir,
      stdio: 'ignore',
    });
    const after = captureGitState(repoDir);
    expect(after).not.toBeNull();
    if (!after) return;

    const mutations = diffAndRestore(before, after, repoDir);
    expect(mutations.some(m => m.includes('origin URL'))).toBe(true);
  });

  it('restores origin URL after change', () => {
    const before = captureGitState(repoDir);
    expect(before).not.toBeNull();
    if (!before) return;
    execSync('git remote set-url origin https://evil.com/repo.git', {
      cwd: repoDir,
      stdio: 'ignore',
    });
    const after = captureGitState(repoDir);
    expect(after).not.toBeNull();
    if (!after) return;

    diffAndRestore(before, after, repoDir);

    const restoredUrl = execSync('git remote get-url origin', {
      cwd: repoDir,
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    expect(restoredUrl).toBe('https://example.com/repo.git');
  });

  it('handles non-repo workspace gracefully — captureGitState returns null', () => {
    const nonRepo = fs.mkdtempSync(path.join(os.tmpdir(), 'non-repo-'));
    try {
      const state = captureGitState(nonRepo);
      expect(state).toBeNull();
    } finally {
      fs.rmSync(nonRepo, { recursive: true, force: true });
    }
  });
});
