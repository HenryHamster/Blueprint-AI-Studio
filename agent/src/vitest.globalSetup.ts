/**
 * Vitest globalSetup: guard against tests mutating real VM git state.
 *
 * Snapshots the git hooks and origin URL for the default AGENT_WORKSPACE
 * (/tmp/workspace) before any tests run, then asserts byte-identical state
 * after the suite completes. Fails loudly naming exactly what changed.
 *
 * The guard intentionally passes when read-only hooks legitimately pre-exist
 * (dispatch installs them before the suite starts) — it only fails when the
 * suite itself creates or mutates git state.
 */
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';

const WATCH_WORKSPACE = '/tmp/workspace';
const BLOCKED_HOOKS = ['pre-commit', 'pre-push'] as const;

interface GitState {
  originUrl: string | null;
  hooks: Record<string, string | null>;
}

function captureGitState(workspace: string): GitState | null {
  const hooksDir = path.join(workspace, '.git', 'hooks');
  const hooks: Record<string, string | null> = {};
  for (const hook of BLOCKED_HOOKS) {
    const hookPath = path.join(hooksDir, hook);
    try {
      hooks[hook] = fs.readFileSync(hookPath, 'utf-8');
    } catch {
      hooks[hook] = null;
    }
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

  return { originUrl, hooks };
}

export function setup(): (() => void) | undefined {
  // Skip if workspace isn't a git repo (e.g. some CI environments).
  let beforeState: GitState | null = null;
  try {
    beforeState = captureGitState(WATCH_WORKSPACE);
  } catch {
    return undefined;
  }

  return () => {
    if (!beforeState) return;

    let afterState: GitState;
    try {
      afterState = captureGitState(WATCH_WORKSPACE) as GitState;
    } catch {
      return;
    }

    const mutations: string[] = [];

    if (beforeState.originUrl !== afterState.originUrl) {
      mutations.push(
        `git remote origin URL changed:\n` +
        `  before: ${beforeState.originUrl ?? '(none)'}\n` +
        `  after:  ${afterState.originUrl ?? '(none)'}`,
      );
    }

    for (const hook of BLOCKED_HOOKS) {
      const before = beforeState.hooks[hook];
      const after = afterState.hooks[hook];
      if (before === after) continue;
      const hookPath = `${WATCH_WORKSPACE}/.git/hooks/${hook}`;
      if (before === null) {
        mutations.push(`${hookPath} was CREATED by the test suite`);
      } else if (after === null) {
        mutations.push(`${hookPath} was DELETED by the test suite`);
      } else {
        mutations.push(`${hookPath} was MODIFIED by the test suite`);
      }
    }

    if (mutations.length > 0) {
      const restoreErrors: string[] = [];

      // Restore hooks to their original state.
      for (const hook of BLOCKED_HOOKS) {
        const hookPath = path.join(WATCH_WORKSPACE, '.git', 'hooks', hook);
        const beforeContent = beforeState.hooks[hook];
        try {
          if (beforeContent === null) {
            // Hook was not present before; delete it if it exists now.
            try {
              fs.unlinkSync(hookPath);
            } catch {
              // Already absent, which is the desired state.
            }
          } else {
            // Hook existed before; restore its content.
            fs.writeFileSync(hookPath, beforeContent, { mode: 0o755 });
          }
        } catch (err) {
          restoreErrors.push(`Failed to restore ${hookPath}: ${(err as Error).message}`);
        }
      }

      // Restore origin URL if it changed.
      if (beforeState.originUrl !== afterState.originUrl && beforeState.originUrl !== null) {
        try {
          execSync(`git remote set-url origin "${beforeState.originUrl}"`, {
            cwd: WATCH_WORKSPACE,
            stdio: 'ignore',
          });
        } catch (err) {
          restoreErrors.push(`Failed to restore origin URL: ${(err as Error).message}`);
        }
      }

      // Throw the mutation error, optionally noting any restore failures.
      const errorMsg =
        `[workspace-guard] Test suite mutated real VM git state in ${WATCH_WORKSPACE}:\n` +
        mutations.map(m => `  - ${m}`).join('\n') +
        '\n\nA test is running a subprocess against the real workspace instead of an\n' +
        'isolated temp dir. Fix: pass AGENT_WORKSPACE pointing to a mkdtempSync\n' +
        'dir in the subprocess invocation.' +
        (restoreErrors.length > 0
          ? '\n\nRestore attempts (best-effort):\n' + restoreErrors.map(e => `  - ${e}`).join('\n')
          : '\n\nState has been restored.');
      throw new Error(errorMsg);
    }
  };
}
