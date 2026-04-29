// Phase 2 — warm-VM continuity workspace preparation.
//
// `prepareWorkspaceForRole` replaces the per-handler `rm -rf WORKSPACE +
// cloneWithRetry` pattern that was wiping the dependency caches resetForNextRole
// works hard to preserve.
//
// Two paths:
//   - Cold path (default): byte-equivalent to the prior `rm + clone` so existing
//     test suites continue to pass.
//   - Warm path (BLUEPRINT_WARM_HANDOFF=1 AND .git already exists): reuse the
//     workspace via `git fetch + checkout + reset --hard + clean -fdx -e ...`,
//     preserving node_modules / .testmondata / .vitest_cache / .pnpm-store.
//
// The role-loop in index.ts sets BLUEPRINT_WARM_HANDOFF=1 *after* a successful
// resetForNextRole and unsets it on cold start, so the first role on every VM
// always takes the cold path.
//
// On any failure of the warm path we log and fall through to the cold path.
// Cold-path semantics MUST match the prior pattern exactly so warm continuity
// can be flipped on/off without test churn.
import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';
import { cloneWithRetry, log } from './index.js';
import { DEFAULT_CLONE_DEPTH } from './constants.js';

export interface PrepareWorkspaceOptions {
  workspace: string;
  /** Authenticated clone URL (caller embeds the ghToken). */
  repoUrl: string;
  /**
   * Raw GitHub token — currently unused but kept in the signature so the
   * helper can rotate the token-less remote URL on the warm path if a future
   * change separates authentication from the URL. Callers always pass it for
   * forward compatibility.
   */
  githubToken: string;
  branch: string;
  baseBranch?: string;
  /**
   * Optional --depth override. Most handlers historically used
   * DEFAULT_CLONE_DEPTH; a few used 100 directly. The cold path falls back
   * to DEFAULT_CLONE_DEPTH when depth is omitted. Pass `0` (or any non-
   * positive number) to perform a FULL clone (no --depth flag) — the
   * research handler relies on full git history.
   */
  depth?: number;
}

/**
 * Prepare the workspace for the next role. Either reuses the existing clone
 * (warm path) or wipes + re-clones (cold path).
 */
export async function prepareWorkspaceForRole(opts: PrepareWorkspaceOptions): Promise<void> {
  const { workspace, repoUrl, branch, baseBranch } = opts;
  // depth: undefined → DEFAULT_CLONE_DEPTH; <=0 → full clone (omit --depth).
  const depthOpt = typeof opts.depth === 'number' && Number.isFinite(opts.depth)
    ? opts.depth
    : DEFAULT_CLONE_DEPTH;

  const warmHandoff = process.env.BLUEPRINT_WARM_HANDOFF === '1';
  const gitDirExists = warmHandoff && fs.existsSync(path.join(workspace, '.git'));

  if (warmHandoff && gitDirExists) {
    try {
      // Update remote URL so we use the freshly minted GitHub token (the prior
      // role's token may have expired) without leaking the old one in the
      // working tree's git config.
      execFileSync('git', ['-C', workspace, 'remote', 'set-url', 'origin', repoUrl], {
        encoding: 'utf-8', timeout: 30_000,
      });
      execFileSync('git', ['-C', workspace, 'fetch', 'origin', branch], {
        encoding: 'utf-8', timeout: 120_000,
      });
      if (baseBranch && baseBranch !== branch) {
        try {
          execFileSync('git', ['-C', workspace, 'fetch', 'origin', baseBranch], {
            encoding: 'utf-8', timeout: 120_000,
          });
        } catch (e) {
          log(`prepareWorkspaceForRole: fetch base-branch ${baseBranch} failed: ${String(e)}`);
        }
      }
      // Try to checkout the branch directly; create from origin if it doesn't
      // exist locally yet (warm VM may have only seen baseBranch so far).
      try {
        execFileSync('git', ['-C', workspace, 'checkout', branch], {
          encoding: 'utf-8', timeout: 30_000,
        });
      } catch {
        execFileSync('git', ['-C', workspace, 'checkout', '-B', branch, `origin/${branch}`], {
          encoding: 'utf-8', timeout: 30_000,
        });
      }
      execFileSync('git', ['-C', workspace, 'reset', '--hard', `origin/${branch}`], {
        encoding: 'utf-8', timeout: 30_000,
      });
      execFileSync(
        'git',
        [
          '-C', workspace,
          'clean', '-fdx',
          '-e', 'node_modules',
          '-e', '.testmondata',
          '-e', '.vitest_cache',
          '-e', '.pnpm-store',
        ],
        { encoding: 'utf-8', timeout: 60_000 },
      );
      log(`prepareWorkspaceForRole: warm-handoff reuse OK (branch=${branch})`);
      return;
    } catch (e) {
      log(`prepareWorkspaceForRole: warm path failed (${String(e)}); falling back to cold clone`);
      // Fall through to cold path.
    }
  }

  // Cold path — byte-equivalent to the prior `rm + clone` pattern.
  execFileSync('rm', ['-rf', workspace], { encoding: 'utf-8' });
  const cloneArgs = depthOpt > 0
    ? ['clone', '--depth', String(depthOpt), '--branch', branch, repoUrl, workspace]
    : ['clone', '--branch', branch, repoUrl, workspace];
  await cloneWithRetry(cloneArgs);
}
