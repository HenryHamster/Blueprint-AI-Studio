/**
 * Vitest globalSetup: guard against tests mutating real VM git state.
 *
 * Snapshots the full .git/config, all hook files, and worktree status
 * for the default AGENT_WORKSPACE (/tmp/workspace) before any tests run,
 * then asserts byte-identical state after the suite completes. Fails loudly
 * naming exactly what changed.
 *
 * The guard intentionally passes when read-only hooks legitimately pre-exist
 * (dispatch installs them before the suite starts) — it only fails when the
 * suite itself creates or mutates git state.
 */
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';

const WATCH_WORKSPACE = '/tmp/workspace';

export interface GitState {
  originUrl: string | null;
  gitConfigContent: string | null;
  hooks: Record<string, string | null>;
  worktreeStatus: string;
}

function readAllHooks(hooksDir: string): Record<string, string | null> {
  const hooks: Record<string, string | null> = {};
  let entries: fs.Dirent[] = [];
  try {
    entries = fs.readdirSync(hooksDir, { withFileTypes: true });
  } catch {
    return hooks;
  }
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    const hookPath = path.join(hooksDir, entry.name);
    try {
      hooks[entry.name] = fs.readFileSync(hookPath, 'utf-8');
    } catch {
      hooks[entry.name] = null;
    }
  }
  return hooks;
}

export function captureGitState(workspace: string): GitState | null {
  const gitDir = path.join(workspace, '.git');
  if (!fs.existsSync(gitDir)) return null;

  const hooksDir = path.join(gitDir, 'hooks');
  const hooks = readAllHooks(hooksDir);

  let gitConfigContent: string | null = null;
  try {
    gitConfigContent = fs.readFileSync(path.join(gitDir, 'config'), 'utf-8');
  } catch {
    gitConfigContent = null;
  }

  let originUrl: string | null = null;
  try {
    originUrl = execSync('git remote get-url origin', {
      cwd: workspace,
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    originUrl = null;
  }

  let worktreeStatus = '';
  try {
    worktreeStatus = execSync('git status --porcelain=v1', {
      cwd: workspace,
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
  } catch {
    worktreeStatus = '';
  }

  return { originUrl, gitConfigContent, hooks, worktreeStatus };
}

export function diffAndRestore(
  before: GitState,
  after: GitState,
  workspace: string,
): string[] {
  const mutations: string[] = [];
  const gitDir = path.join(workspace, '.git');
  const hooksDir = path.join(gitDir, 'hooks');

  if (before.gitConfigContent !== after.gitConfigContent) {
    mutations.push(
      `.git/config was MODIFIED by the test suite`,
    );
  }

  if (before.originUrl !== after.originUrl) {
    mutations.push(
      `git remote origin URL changed:\n` +
      `  before: ${before.originUrl ?? '(none)'}\n` +
      `  after:  ${after.originUrl ?? '(none)'}`,
    );
  }

  const allHookNames = new Set([
    ...Object.keys(before.hooks),
    ...Object.keys(after.hooks),
  ]);
  for (const hook of allHookNames) {
    const beforeContent = before.hooks[hook] ?? null;
    const afterContent = after.hooks[hook] ?? null;
    if (beforeContent === afterContent) continue;
    const hookPath = path.join(hooksDir, hook);
    if (beforeContent === null) {
      mutations.push(`${hookPath} was CREATED by the test suite`);
    } else if (afterContent === null) {
      mutations.push(`${hookPath} was DELETED by the test suite`);
    } else {
      mutations.push(`${hookPath} was MODIFIED by the test suite`);
    }
  }

  if (before.worktreeStatus !== after.worktreeStatus) {
    const beforeLines = new Set(before.worktreeStatus.split('\n').filter(Boolean));
    const afterLines = after.worktreeStatus.split('\n').filter(Boolean);
    const newLines = afterLines.filter(l => !beforeLines.has(l));
    if (newLines.length > 0) {
      mutations.push(
        `worktree stray files created by the test suite:\n` +
        newLines.map(l => `  ${l}`).join('\n'),
      );
    } else {
      mutations.push(`worktree status changed:\n  before: ${before.worktreeStatus.trim()}\n  after: ${after.worktreeStatus.trim()}`);
    }
  }

  if (mutations.length === 0) return mutations;

  const restoreErrors: string[] = [];

  // Restore .git/config if it changed.
  if (before.gitConfigContent !== after.gitConfigContent && before.gitConfigContent !== null) {
    try {
      fs.writeFileSync(path.join(gitDir, 'config'), before.gitConfigContent, { mode: 0o644 });
    } catch (err) {
      restoreErrors.push(`Failed to restore .git/config: ${(err as Error).message}`);
    }
  }

  // Restore hooks to their original state.
  for (const hook of allHookNames) {
    const hookPath = path.join(hooksDir, hook);
    const beforeContent = before.hooks[hook] ?? null;
    const afterContent = after.hooks[hook] ?? null;
    if (beforeContent === afterContent) continue;
    try {
      if (beforeContent === null) {
        try { fs.unlinkSync(hookPath); } catch { /* already absent */ }
      } else {
        fs.writeFileSync(hookPath, beforeContent, { mode: 0o755 });
      }
    } catch (err) {
      restoreErrors.push(`Failed to restore ${hookPath}: ${(err as Error).message}`);
    }
  }

  // Restore origin URL if it changed.
  if (before.originUrl !== after.originUrl && before.originUrl !== null) {
    try {
      execSync(`git remote set-url origin "${before.originUrl}"`, {
        cwd: workspace,
        stdio: 'ignore',
      });
    } catch (err) {
      restoreErrors.push(`Failed to restore origin URL: ${(err as Error).message}`);
    }
  }

  // Delete stray files created by the suite (safe — they weren't tracked before).
  if (before.worktreeStatus !== after.worktreeStatus) {
    const beforeLines = new Set(before.worktreeStatus.split('\n').filter(Boolean));
    const afterLines = after.worktreeStatus.split('\n').filter(Boolean);
    for (const line of afterLines) {
      if (beforeLines.has(line)) continue;
      // porcelain=v1: "XY path" or "XY orig -> path" — untracked lines start with "??"
      const match = line.match(/^\?\? (.+)$/);
      if (match) {
        const strayPath = path.join(workspace, match[1]);
        try { fs.rmSync(strayPath, { recursive: true, force: true }); } catch { /* ignore */ }
      }
    }
  }

  return mutations;
}

export function setup(): (() => void) | undefined {
  // Skip if workspace isn't a git repo (e.g. some CI environments).
  let beforeState: GitState | null = null;
  try {
    beforeState = captureGitState(WATCH_WORKSPACE);
  } catch {
    return undefined;
  }

  if (!beforeState) return undefined;

  const capturedBefore: GitState = beforeState;

  return () => {
    let afterState: GitState | null;
    try {
      afterState = captureGitState(WATCH_WORKSPACE);
    } catch {
      return;
    }

    if (!afterState) return;

    const mutations = diffAndRestore(capturedBefore, afterState, WATCH_WORKSPACE);

    if (mutations.length > 0) {
      const errorMsg =
        `[workspace-guard] Test suite mutated real VM git state in ${WATCH_WORKSPACE}:\n` +
        mutations.map(m => `  - ${m}`).join('\n') +
        '\n\nA test is running a subprocess against the real workspace instead of an\n' +
        'isolated temp dir. Fix: pass AGENT_WORKSPACE pointing to a mkdtempSync\n' +
        'dir in the subprocess invocation.';
      throw new Error(errorMsg);
    }
  };
}
