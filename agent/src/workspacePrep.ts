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
import { DEFAULT_CLONE_DEPTH } from './constants.js';
import { sanitize } from './utils/sanitize.js';

/**
 * Resolve the pre-baked git object mirror path for an authenticated clone URL,
 * or null if the URL can't be parsed into an owner/repo. The image seeds a
 * mirror at `${GIT_REFERENCE_CACHE_ROOT}/<owner>/<repo>.git` (path layout matches
 * setupWorkspace in index.ts). Returns the path only when the mirror actually
 * exists on disk so callers can append `--reference-if-able` safely.
 */
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
let _lastWorkspacePrepOutcome: WorkspacePrepOutcome | null = null;

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

  // lane_phase is meaningful for both warm and cold paths — computed upfront.
  const allowLaneResume = opts.allowLaneResume !== false;
  const lanePhase = allowLaneResume && process.env.BLUEPRINT_LANE_MODE === '1';

  const warmHandoff = process.env.BLUEPRINT_WARM_HANDOFF === '1';
  const gitDirExists = warmHandoff && fs.existsSync(path.join(workspace, '.git'));

  // cold_fallback_reason is set in the warm-path catch when we fall through.
  let coldFallbackReason: string | null = null;

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
      // the checkpoint SHA. If the SHA is unreachable (force-push or orphaned ref)
      // the reset throws and the outer catch falls back to a full cold clone.
      // allowLaneResume defaults true (env-driven). A reviewer/auditor passes
      // false so lane reset can never engage even if a lane env leaked onto a
      // cold claim — the cold, independent review path is non-negotiable.
      // lanePhase is computed above (before the warm/cold branch).
      const laneCheckpointSha = lanePhase ? (process.env.BLUEPRINT_LANE_CHECKPOINT_SHA ?? '') : '';
      const checkpointResetApplied = lanePhase && !!laneCheckpointSha;
      if (lanePhase && laneCheckpointSha) {
        execFileSync('git', ['-C', workspace, 'reset', '--hard', laneCheckpointSha], {
          encoding: 'utf-8', timeout: 30_000,
        });
        log(`prepareWorkspaceForRole: lane-mode reset to checkpoint ${laneCheckpointSha} (branch=${branch})`);
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
  execFileSync('rm', ['-rf', workspace], { encoding: 'utf-8' });
  const cloneArgs = depthOpt > 0
    ? ['clone', '--depth', String(depthOpt), '--branch', branch]
    : ['clone', '--branch', branch];
  const referenceRepo = resolveReferenceRepo(repoUrl);
  if (referenceRepo) {
    cloneArgs.push('--reference-if-able', referenceRepo);
    log(`prepareWorkspaceForRole: using pre-baked git object cache at ${referenceRepo}`);
  }
  cloneArgs.push(repoUrl, workspace);
  await cloneWithRetry(cloneArgs);
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
