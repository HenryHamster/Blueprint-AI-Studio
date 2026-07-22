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
import { cloneWithRetry, log, GIT_REFERENCE_CACHE_ROOT, registerSizeCapMergeDriver } from './index.js';
import type { FailureCategory } from './index.js';
import { DEFAULT_CLONE_DEPTH } from './constants.js';
import { sanitize } from './utils/sanitize.js';
import {
  writeGitTokenFile,
  setupGitCredentialHelper,
  ensureGitCredentialHelperScript,
  gitCredentialHelperPath,
} from './git-credential.js';

/**
 * Resolve the pre-baked git object mirror path for an authenticated clone URL,
 * or null if the URL can't be parsed into an owner/repo. The image seeds a
 * mirror at `${GIT_REFERENCE_CACHE_ROOT}/<owner>/<repo>.git` (path layout matches
 * setupWorkspace in index.ts). Returns the path only when the mirror actually
 * exists on disk so callers can append `--reference-if-able` safely.
 */
/**
 * List the top-level directory names of a prepared workspace — the
 * project-shape-agnostic seed for the strategist's `code:<dir>`
 * examination surfaces (no hardcoded project types). Skips dotfiles/`.git` and
 * non-directories; returns [] on any fs error so a scan failure at session start
 * never breaks the strategic loop.
 */
export function listWorkspaceTopDirs(workspace: string): string[] {
  try {
    return fs.readdirSync(workspace, { withFileTypes: true })
      .filter(e => e.isDirectory() && !e.name.startsWith('.'))
      .map(e => e.name)
      .sort();
  } catch {
    return [];
  }
}

export function resolveReferenceRepo(repoUrl: string): string | null {
  let pathname: string;
  try {
    pathname = new URL(repoUrl).pathname; // → '/owner/repo.git'
  } catch {
    return null;
  }
  const ownerRepo = pathname.replace(/^\/+/, '').replace(/\.git$/, '');
  if (!ownerRepo || ownerRepo.split('/').length !== 2) return null;
  const referenceRepo = `${GIT_REFERENCE_CACHE_ROOT}/${ownerRepo}.git`;
  return fs.existsSync(referenceRepo) ? referenceRepo : null;
}

export interface PrepareWorkspaceOptions {
  workspace: string;
  /**
   * Repo URL — may be a plain `https://github.com/owner/repo.git` or a
   * legacy token-embedded `https://x-access-token:<tok>@github.com/…` form.
   * Owner/repo are extracted from the URL; the embedded token (if any) is
   * never forwarded to git — authentication goes through the credential helper.
   */
  repoUrl: string;
  /**
   * Raw GitHub token written to the on-disk credential-helper token file
   * so git operations can authenticate without embedding the secret in the URL.
   */
  githubToken: string;
  branch: string;
  baseBranch?: string;
  /**
   * Optional --depth override. Most handlers historically used
   * DEFAULT_CLONE_DEPTH; a few used 100 directly. The cold path falls back
   * to DEFAULT_CLONE_DEPTH when depth is omitted. Pass `0` (or any non-
   * positive number) to perform a FULL clone (no --depth flag) — the
   * research handler relies on full git history, and the release qualifier
   * (release_audit / qualifier-smoketest) needs the merge-base reachable.
   * A non-positive depth is also honored on the WARM path: if the reused
   * workspace is shallow (a `.git/shallow` left by an earlier role's shallow
   * clone) the helper runs `git fetch --unshallow` so full history is
   * present. Shallow (depth > 0) requests never trigger the unshallow.
   */
  depth?: number;
  /**
   * Review-fix lane pilot, gate-integrity guard. Lane warm-resume
   * (hard-reset to BLUEPRINT_LANE_CHECKPOINT_SHA) lane-izes ONLY the FIX
   * side of the review loop. A reviewer/auditor must NEVER resume onto the
   * implementer's lane workspace — that would destroy review independence.
   * Defaults to `true` (env-driven, unchanged behavior); the review/audit
   * handlers pass `false` to hard-disable lane reset regardless of any env
   * the backend should never have set on a cold claim. Defense in depth.
   */
  allowLaneResume?: boolean;
}

/** Structured outcome from a single `prepareWorkspaceForRole` call. */
export interface WorkspacePrepOutcome {
  /** Which path actually completed. */
  path: 'warm' | 'cold';
  /** True only when the warm reuse path completed without falling back to cold. */
  warm_reuse_succeeded: boolean;
  /** True only when lane-mode hard-reset to the checkpoint SHA actually executed. */
  checkpoint_reset_applied: boolean;
  /**
   * Short bounded reason string when the cold path was taken AFTER a warm
   * attempt failed. Null when no warm attempt was made (genuine cold start) or
   * when warm succeeded.
   */
  cold_fallback_reason: string | null;
  /** Whether lane mode was requested (BLUEPRINT_LANE_MODE=1 with allowLaneResume). */
  lane_phase: boolean;
}

// Module-level recorder: written by prepareWorkspaceForRole, read by
// enrichStatusPayload (via getLastWorkspacePrepOutcome). Reset between roles.
let _lastWorkspacePrepOutcome: WorkspacePrepOutcome | null = null; // @per-role

/** Returns the outcome of the most recent prepareWorkspaceForRole call, or
 *  null if the function has not been called since the last reset. */
export function getLastWorkspacePrepOutcome(): WorkspacePrepOutcome | null {
  return _lastWorkspacePrepOutcome;
}

/** Reset the recorded outcome (called between roles by resetPerRoleModuleState). */
export function resetWorkspacePrepOutcome(): void {
  _lastWorkspacePrepOutcome = null;
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

  // Extract owner/repo from the URL (handles both clean and legacy token-embedded forms).
  // Fall back to the original repoUrl if parsing fails so no existing path breaks.
  let cleanRepoUrl = repoUrl;
  let ownerRepo: { owner: string; repo: string } | null = null;
  try {
    const pathname = new URL(repoUrl).pathname; // '/owner/repo.git' (ignores userinfo)
    const parts = pathname.replace(/^\/+/, '').replace(/\.git$/, '').split('/');
    if (parts.length === 2 && parts[0] && parts[1]) {
      ownerRepo = { owner: parts[0], repo: parts[1] };
      cleanRepoUrl = `https://github.com/${parts[0]}/${parts[1]}.git`;
    }
  } catch { /* malformed URL — use repoUrl as-is */ }

  // Write the GitHub token to the on-disk credential-helper file before any
  // git network operation so the helper always finds a valid token.
  if (opts.githubToken) {
    writeGitTokenFile(opts.githubToken);
  }

  // lane_phase is meaningful for both warm and cold paths — computed upfront.
  const allowLaneResume = opts.allowLaneResume !== false;
  const lanePhase = allowLaneResume && process.env.BLUEPRINT_LANE_MODE === '1';

  const warmHandoff = process.env.BLUEPRINT_WARM_HANDOFF === '1';
  const gitDirExists = warmHandoff && fs.existsSync(path.join(workspace, '.git'));

  // cold_fallback_reason is set in the warm-path catch when we fall through.
  let coldFallbackReason: string | null = null;

  if (warmHandoff && gitDirExists) {
    try {
      // Rotate credentials without embedding the token in the remote URL:
      // configure the credential helper in the workspace git config and set
      // the remote to a plain HTTPS URL. The helper reads the fresh token
      // written above on every git network call.
      if (ownerRepo) {
        setupGitCredentialHelper(ownerRepo.owner, ownerRepo.repo, workspace);
      } else {
        // Fallback: could not parse owner/repo — update URL as before.
        execFileSync('git', ['-C', workspace, 'remote', 'set-url', 'origin', repoUrl], {
          encoding: 'utf-8', timeout: 30_000,
        });
      }
      execFileSync('git', ['-C', workspace, 'fetch', 'origin', branch], {
        encoding: 'utf-8', timeout: 120_000,
      });
      // Full-clone request on a shallow warm workspace: the cold path honors
      // `depthOpt <= 0` by omitting --depth, but the warm path's plain
      // `git fetch origin <branch>` keeps whatever shallow boundary role 1's
      // clone left behind (a `.git/shallow` file). Callers that pass depth 0
      // (release_audit's `main...<candidate_sha>` three-dot diff,
      // qualifier-smoketest's candidate-SHA fetch+checkout) need the merge-base
      // reachable, so unshallow the repo here. We only do this when the request
      // is a full clone AND the repo is actually shallow — a non-positive depth
      // on an already-complete repo is a no-op, and shallow (depth > 0) requests
      // are untouched, so the cold path and every shallow handler are
      // unaffected.
      if (depthOpt <= 0 && fs.existsSync(path.join(workspace, '.git', 'shallow'))) {
        try {
          execFileSync('git', ['-C', workspace, 'fetch', '--unshallow', 'origin', branch], {
            encoding: 'utf-8', timeout: 300_000,
          });
          log(`prepareWorkspaceForRole: unshallowed warm workspace (branch=${branch}) for full-clone request`);
        } catch (e) {
          // `git fetch --unshallow` errors "fatal: --unshallow on a complete
          // repository does not make sense" if the repo became complete
          // between the .git/shallow check and the fetch (e.g. a concurrent
          // fetch). That specific case is benign — the repo already has full
          // history, which is exactly what a full-clone request wants — so
          // swallow it. Any other failure is a real warm-path failure and
          // must propagate to the outer catch so we fall back to a cold full
          // clone rather than handing the handler a still-shallow repo.
          const msg = String(e);
          if (/--unshallow on a complete repository/i.test(msg)) {
            log(`prepareWorkspaceForRole: warm workspace already complete (branch=${branch}); --unshallow skipped`);
          } else {
            throw e;
          }
        }
      }
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
      // Lane workspace-mode: treat the checkpoint SHA as source of truth rather
      // than resetting to origin/<branch>. The checkpoint is the last verified
      // commit; origin/<branch> may be ahead (another phase pushed). Any mismatch
      // (external push, leftover residue, divergent HEAD) is fixed by hard-reset to
      // the checkpoint SHA. Before resetting, verify the checkpoint SHA is still an
      // ancestor of the remote branch. On a warm workspace, a force-push may have
      // orphaned the commit, but it could still exist locally as a dangling object,
      // so reset would succeed even though the commit is no longer reachable. If the
      // SHA is not an ancestor, fall back to resetting to origin/<branch> instead.
      // allowLaneResume defaults true (env-driven). A reviewer/auditor passes
      // false so lane reset can never engage even if a lane env leaked onto a
      // cold claim — the cold, independent review path is non-negotiable.
      // lanePhase is computed above (before the warm/cold branch).
      const laneCheckpointSha = lanePhase ? (process.env.BLUEPRINT_LANE_CHECKPOINT_SHA ?? '') : '';
      let checkpointResetApplied = false;
      if (lanePhase && laneCheckpointSha) {
        // Verify the checkpoint SHA is still an ancestor of the remote branch.
        let isAncestor = false;
        try {
          execFileSync('git', ['-C', workspace, 'merge-base', '--is-ancestor', laneCheckpointSha, `origin/${branch}`], {
            encoding: 'utf-8', timeout: 30_000,
          });
          isAncestor = true;
        } catch (err) {
          // merge-base --is-ancestor exits with status 1 when the first arg is not an
          // ancestor of the second (empty stderr). That's the normal "not an ancestor"
          // case and is handled below. Errors (exit > 1) propagate to trigger cold fallback.
          if ((err as NodeJS.ErrnoException & { status?: number }).status !== 1) {
            throw err;
          }
        }
        if (isAncestor) {
          execFileSync('git', ['-C', workspace, 'reset', '--hard', laneCheckpointSha], {
            encoding: 'utf-8', timeout: 30_000,
          });
          log(`prepareWorkspaceForRole: lane-mode reset to checkpoint ${laneCheckpointSha} (branch=${branch})`);
          checkpointResetApplied = true;
        } else {
          // Checkpoint SHA is not an ancestor (force-push / orphaned).
          // Fall back to resetting to origin/<branch> instead.
          log(`prepareWorkspaceForRole: checkpoint ${laneCheckpointSha} not ancestor of origin/${branch}; resetting to origin/${branch} instead`);
          execFileSync('git', ['-C', workspace, 'reset', '--hard', `origin/${branch}`], {
            encoding: 'utf-8', timeout: 30_000,
          });
        }
      } else {
        execFileSync('git', ['-C', workspace, 'reset', '--hard', `origin/${branch}`], {
          encoding: 'utf-8', timeout: 30_000,
        });
      }
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
      // Re-register the size-cap merge driver: git config lives in .git/config,
      // which the warm reuse keeps, but registering is idempotent and cheap so we
      // always assert it (a prior role on a different repo could have left it unset).
      registerSizeCapMergeDriver(workspace);
      log(`prepareWorkspaceForRole: warm-handoff reuse OK (branch=${branch})`);
      _lastWorkspacePrepOutcome = {
        path: 'warm',
        warm_reuse_succeeded: true,
        checkpoint_reset_applied: checkpointResetApplied,
        cold_fallback_reason: null,
        lane_phase: lanePhase,
      };
      return;
    } catch (e) {
      const sanitizedError = sanitize(String(e));
      coldFallbackReason = `warm_path_error: ${sanitizedError.slice(0, 200)}`;
      log(`prepareWorkspaceForRole: warm path failed (${sanitizedError}); falling back to cold clone`);
      // Fall through to cold path.
    }
  }

  // Cold path — `rm + clone`, plus reuse of the pre-baked git object mirror
  // when the image seeded one for this repo. `--reference-if-able` borrows
  // objects from the local mirror and silently falls back to a normal fetch if
  // the path is missing or invalid, so cold starts for every role (review-fix /
  // specialized / research / design-doc) get the clone speedup, not just
  // setupWorkspace's standard path.
  //
  // Empty contents in place rather than removing the directory itself.
  // Under bwrap the workspace is a bind-mount point: `rm -rf` on the
  // mountpoint returns EBUSY, but removing its children is legal. `git clone`
  // accepts an existing empty directory, so semantics are equivalent for
  // the non-sandboxed case too — it keeps whatever dir already exists.
  try {
    const entries = fs.readdirSync(workspace);
    for (const entry of entries) {
      fs.rmSync(path.join(workspace, entry), { recursive: true, force: true });
    }
  } catch (e) {
    // ENOENT: directory doesn't exist yet — git clone will create it.
    if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e;
  }
  // Write the credential helper script before the clone so the `-c` option
  // below can point git at it for authentication during the clone itself.
  const helperPath = ownerRepo ? ensureGitCredentialHelperScript() : gitCredentialHelperPath();
  const cloneArgs: string[] = ownerRepo
    ? ['-c', `credential.https://github.com.helper=${helperPath}`]
    : [];
  if (depthOpt > 0) {
    cloneArgs.push('clone', '--depth', String(depthOpt), '--branch', branch);
  } else {
    cloneArgs.push('clone', '--branch', branch);
  }
  const referenceRepo = resolveReferenceRepo(cleanRepoUrl);
  if (referenceRepo) {
    cloneArgs.push('--reference-if-able', referenceRepo);
    log(`prepareWorkspaceForRole: using pre-baked git object cache at ${referenceRepo}`);
  }
  cloneArgs.push(cleanRepoUrl, workspace);
  try {
    await cloneWithRetry(cloneArgs);
  } catch (e) {
    const err = e instanceof Error ? e : new Error(String(e));
    (err as Error & { failureCategory?: FailureCategory }).failureCategory = 'clone_failed';
    throw err;
  }
  // Configure the credential helper and clean remote URL in the workspace git
  // config so all subsequent operations (fetch, push, ls-remote) authenticate
  // via the helper and never embed the token in .git/config.
  if (ownerRepo) {
    setupGitCredentialHelper(ownerRepo.owner, ownerRepo.repo, workspace);
  }
  // A fresh clone does NOT carry .git/config, so the .gitattributes sizecap
  // mapping is inert until the driver command is registered here.
  registerSizeCapMergeDriver(workspace);
  _lastWorkspacePrepOutcome = {
    path: 'cold',
    warm_reuse_succeeded: false,
    checkpoint_reset_applied: false,
    cold_fallback_reason: coldFallbackReason,
    lane_phase: lanePhase,
  };
}
