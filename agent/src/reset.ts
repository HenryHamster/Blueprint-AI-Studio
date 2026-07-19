// Phase 1 — warm-VM continuity reset routine.
//
// `resetForNextRole` runs between roles on a single warm VM, wiping all
// per-role state while preserving the cloned workspace + dependency caches
// so the next role starts in seconds rather than re-paying VM-provisioning
// + clone + npm install (~3 min).
//
// SPIKE — Claude Code session state path
// --------------------------------------
// Open question 1 from the plan: where does Claude Code store session
// state? Confirmed `$HOME/.claude/`.
//
// Evidence in this repo:
//   1. agent/src/index.ts:2299  — Claude Code is invoked as the bare CLI
//      `command: 'claude'` (no CLAUDE_HOME / no --config-dir flag).
//   2. providers/runners.ts:buildBaseCliEnv() sets
//      `HOME: process.env.HOME || os.homedir()` for the subprocess; that
//      is the home Claude Code resolves `~/.claude` against.
//   3. agent/src/prompt.ts:34   — workspace-scoped settings live at
//      `<WORKSPACE>/.claude/settings.json` (per-task tool restrictions).
//      Those are wiped by step 4 (`git clean`) below; the user-scope
//      session state at `~/.claude/` is what step 3 targets.
//
// What `~/.claude/` contains for Claude Code:
//   - `~/.claude/projects/` — per-project conversation history (the bias
//     vector we must wipe between roles)
//   - `~/.claude/sessions/` — session-id state and resumable state
//   - `~/.claude/todos/` — Claude's TodoWrite cache
//   - cached statsig flags + telemetry
//
// We use Node's `fs.rm(..., { recursive: true, force: true })` rather
// than shelling to `rm -rf` — auto-memory rule "No rm -rf" plus the
// shell variant has historically swallowed errors silently.

import fs from 'fs';
import os from 'os';
import path from 'path';
import { execFile } from 'child_process';
import { promisify } from 'util';
import {
  killAllChildren,
  clearPidRegistry,
} from './processTracking.js';

const execFileAsync = promisify(execFile);

// /tmp scratch-file names for all per-task handler outputs.
// This is the single source of truth — both resetForNextRole (below) and
// the role-loop finally block in index.ts import this constant so the two
// cleanup paths can never diverge.
//
// panel_review writes one scratch file per declared lens
// (panelReviewLensOutputType in handlers/panel-review.ts → PANEL_REVIEW_LENSES);
// keep those in sync with that lens set.
export const TMP_OUTPUT_NAMES = [
  'review_verdict',
  'panel_review_lens_correctness',
  'panel_review_lens_security',
  'panel_review_lens_code_quality',
  'panel_review_lens_test_coverage',
  'plan_output',
  'research_findings',
  'security_findings',
  'smoketest_result',
  'verify_tests_result',
  'debug_report',
  'perf_report',
  'investigation_report',
  'strategic_output',
  'design_document',
  'bootstrap_overview',
  'command_memory',
  'web_research_document',
  'release_audit_report',
  'release_audit_challenge',
  'release_merge_grade',
  'self_audit_findings',
  'self_review',
  'symbols',
  'fix_responses',
  'fix_sr_context',
  'test_baseline',
  'fix_parallelized',
  'bootstrap_domain',
  'research_insights',
  'consumed_artifacts',
  'problem_model',
];

export interface ResetOptions {
  /** Workspace directory (the cloned repo). Must exist. */
  workspace: string;
  /** Task ID of the role just completed; used to compute /tmp filenames. */
  priorTaskId: string;
  /**
   * Optional base branch to fast-forward against between roles. When
   * provided, we `git fetch origin <baseBranch>` then `git rebase
   * origin/<baseBranch>` before handing off to the next role.
   */
  baseBranch?: string;
}

/**
 * Raised before a warm handoff attempts to run git in a workspace that no
 * longer exists.  Keeping this distinct from an exec failure makes a failed
 * clone/removal immediately diagnosable instead of surfacing as `spawn git
 * ENOENT` (which incorrectly suggests the git binary is missing).
 */
export class WorkspaceMissingError extends Error {
  constructor(workspace: string) {
    super(`workspace missing at ${workspace} — cannot hand off to next role`);
    this.name = 'WorkspaceMissingError';
  }
}

// Home-relative provider credential / CLI-state paths the boot manifest
// materializes (build_agent_secret_manifest → materialize_manifest.py): provider
// API/OAuth cred dirs plus the Claude Code onboarding-bypass file ~/.claude.json
// and ~/.gemini/settings.json. These are ACCOUNT-SCOPED, not per-task, so they
// must be wiped ONLY when the next role binds a DIFFERENT account — and on that
// account-divergent path performWarmRetarget (index.ts) first materializes the
// new account's manifest (overwriting every same-path credential in place) and
// only THEN clears the stale leftovers via clearStaleProviderCredentials. The
// write-first ordering means there is never a configless instant: the 2026-06-06
// poisoned-VM incidents (tasks 1330d291 / cf28097f) came from the old
// clear-then-write ordering dying after the clear, leaving a VM with no
// ~/.claude.json that kept claiming tasks which all died in seconds with
// "Claude configuration file not found".
//
// Critically, this wipe must NOT run on the same-account fast path (chain
// continuity / backlog reuse, both default-ON). There the boot-materialized
// config is exactly the right config for the next role and nothing re-creates
// it — wiping it would leave role 2+ without ~/.claude.json / ~/.gemini settings
// (buildBaseCliEnv has no env-only equivalent for the onboarding bypass), a
// silent credential/config regression on the live warm paths. resetForNextRole
// therefore does NOT clear these; only the account-divergent re-target does.
const PROVIDER_CRED_HOME_PATHS = [
  '.anthropic',
  '.openai',
  '.gemini',
  '.claude.json',
] as const;

/** Remove a path, falling back to `sudo rm -rf` when the agent user lacks
 *  direct permission (e.g. a root-owned credential file from a failed chown).
 *  All failures are swallowed (logged) — credential clearing is best-effort
 *  and must never abort the between-role reset. */
async function rmBestEffort(target: string): Promise<void> {
  try {
    await fs.promises.rm(target, { recursive: true, force: true });
    return;
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code === 'EACCES' || code === 'EPERM') {
      try {
        await execFileAsync('sudo', ['rm', '-rf', target], { timeout: 30_000 });
        return;
      } catch (sudoErr) {
        console.error(`[reset] clearStaleProviderCredentials sudo rm ${target}: ${String(sudoErr)}`);
        return;
      }
    }
    console.error(`[reset] clearStaleProviderCredentials rm ${target}: ${String(err)}`);
  }
}

/**
 * Clear the prior account's credential material that the NEW account's manifest
 * did not overwrite, so it cannot leak into the next role.
 *
 * Called by the warm re-target path (index.ts performWarmRetarget) AFTER the new
 * account's manifest has been materialized — write-new-then-swap ordering. Every
 * same-path credential (e.g. /etc/agent.env, ~/.claude.json on an
 * anthropic→anthropic re-target) is already overwritten in place by
 * materialize_manifest.py, so this only needs to remove the provider home paths
 * the new manifest does NOT own (e.g. a stale ~/.claude.json when re-targeting
 * to a gemini account). `preservePaths` is the new manifest's file list: a
 * provider home path is kept when it IS, or CONTAINS, a preserved path (so
 * ~/.gemini survives when the manifest owns ~/.gemini/settings.json).
 *
 * The systemd env file is intentionally NOT touched here — materialize rewrote
 * it wholesale, and removing it is exactly the configless window this ordering
 * exists to close. It is deliberately NOT called from resetForNextRole's
 * same-account fast path (see the PROVIDER_CRED_HOME_PATHS comment): there the
 * boot-materialized config is still correct and nothing would re-create it.
 */
export async function clearStaleProviderCredentials(
  preservePaths: readonly string[],
  homeDir: string = process.env.HOME || os.homedir(),
): Promise<void> {
  const preserved = preservePaths.map(p => path.resolve(p));
  for (const rel of PROVIDER_CRED_HOME_PATHS) {
    const target = path.resolve(path.join(homeDir, rel));
    const ownedByNewManifest = preserved.some(
      p => p === target || p.startsWith(target + path.sep),
    );
    if (ownedByNewManifest) continue;
    await rmBestEffort(target);
  }
}

/**
 * Reset all per-role state on the warm VM.
 *
 * Operations are independent: a failure in one step is logged but does
 * not abort subsequent steps. Throws only if step 4 (git reset) fails so
 * badly that we cannot continue (e.g. workspace doesn't exist).
 */
export async function resetForNextRole(opts: ResetOptions): Promise<void> {
  const { workspace, priorTaskId, baseBranch } = opts;

  // Step 1 — kill leftover child processes from the prior role.
  try {
    const { killed, alreadyDead, unkillable } = killAllChildren('SIGKILL');
    if (killed.length || alreadyDead.length || unkillable.length) {
      console.log(
        `[reset] killAllChildren: signalled ${String(killed.length)} pid(s), ${String(alreadyDead.length)} already gone` +
          (unkillable.length > 0
            ? `, ${String(unkillable.length)} unkillable (EPERM)`
            : ''),
      );
    }
    clearPidRegistry();
  } catch (err) {
    console.error(`[reset] step 1 (killAllChildren) failed: ${String(err)}`);
  }

  // Step 2 — remove explicit /tmp blueprint scratch files for the prior
  // task. We list paths defensively (NOT a glob) so we never blow away
  // unrelated /tmp content.
  try {
    const tmpPaths = [`/tmp/agent_prompt_${priorTaskId}.txt`];
    for (const name of TMP_OUTPUT_NAMES) {
      tmpPaths.push(`/tmp/${name}_${priorTaskId}.json`);
    }
    for (const p of tmpPaths) {
      try {
        await fs.promises.unlink(p);
      } catch (err) {
        const code = (err as NodeJS.ErrnoException).code;
        if (code === 'ENOENT') continue; // Missing files are fine.
        console.error(`[reset] step 2 unlink ${p}: ${String(err)}`);
      }
    }
    // review-fix.ts / panel-review.ts download implementer screenshots into
    // per-task directories (directories, not flat files), so they cannot be
    // handled by the unlink loop above. handleReview/panel_review use
    // /tmp/viewport_review_<task>; handleFix uses /tmp/viewport_fix_<task>.
    for (const dir of [`/tmp/viewport_review_${priorTaskId}`, `/tmp/viewport_fix_${priorTaskId}`]) {
      try {
        await fs.promises.rm(dir, { recursive: true, force: true });
      } catch (err) {
        console.error(`[reset] step 2 rm ${dir}: ${String(err)}`);
      }
    }
  } catch (err) {
    console.error(`[reset] step 2 (/tmp cleanup) failed: ${String(err)}`);
  }

  // Step 3 — wipe Claude Code session state at `~/.claude/`. See spike
  // comment at top of file for path provenance.
  //
  // The wipe target MUST match the HOME the spawned Claude Code subprocess
  // sees (runners.ts:buildBaseCliEnv → `HOME: process.env.HOME || os.homedir()`).
  // os.homedir() can resolve via getpwuid() and disagree with the env-set
  // HOME (containers, sudo invocations, /etc/passwd mismatches), which would
  // mean we wipe the wrong directory and the next role inherits the prior
  // role's session state — the exact bias `clearToolTrace` + this wipe are
  // meant to prevent.
  try {
    const homeDir = process.env.HOME || os.homedir();
    const claudeHome = path.join(homeDir, '.claude');
    await fs.promises.rm(claudeHome, { recursive: true, force: true });
  } catch (err) {
    console.error(`[reset] step 3 (~/.claude wipe) failed: ${String(err)}`);
  }

  // NOTE: provider credential material (/etc/agent.env + provider cred/state
  // dirs) is intentionally NOT cleared here. Those files are account-scoped and
  // identical across same-account roles (chain continuity / backlog reuse, both
  // default-ON); wiping them on the same-account fast path would destroy
  // boot-materialized config (~/.claude.json onboarding bypass, ~/.gemini
  // settings) that nothing re-creates, breaking role 2+. Account-divergent warm
  // claims instead materialize-then-clear-stale via performWarmRetarget
  // (index.ts → clearStaleProviderCredentials) before the next role runs, so
  // there is no cross-account leakage on the only path where a leak is possible
  // — and no configless window if the re-target dies mid-sequence.

  // Step 4 — reset the workspace to a clean HEAD while preserving
  // dependency caches. This is the only step we re-throw on; if the
  // workspace itself is gone we cannot hand off to the next role.
  try {
    await fs.promises.stat(workspace);
  } catch {
    const workspaceError = new WorkspaceMissingError(workspace);
    console.error(`[reset] step 4 (workspace check) failed: ${workspaceError.message}`);
    throw workspaceError;
  }
  try {
    await execFileAsync('git', ['reset', '--hard', 'HEAD'], { cwd: workspace });
  } catch (err) {
    console.error(`[reset] step 4 (git reset --hard) failed: ${String(err)}`);
    throw err;
  }
  try {
    // -fdx: force, directories, ignored files (build outputs etc).
    // -e <pat>: exclude these paths from the clean. Preserves caches:
    //   node_modules     — npm/pnpm install output (~60s to rebuild)
    //   .testmondata     — pytest-testmon incremental cache
    //   .vitest_cache    — vitest's transform cache
    //   .pnpm-store      — pnpm's content-addressable store
    await execFileAsync(
      'git',
      [
        'clean', '-fdx',
        '-e', 'node_modules',
        '-e', '.testmondata',
        '-e', '.vitest_cache',
        '-e', '.pnpm-store',
      ],
      { cwd: workspace },
    );
  } catch (err) {
    console.error(`[reset] step 4 (git clean) failed: ${String(err)}`);
  }

  // Step 5 — fast-forward against the base branch if asked. The base
  // branch may have moved between roles and we want the next role to
  // operate on the latest tip. Rebase failure is non-fatal: we abort
  // the rebase and let the role start from current HEAD.
  if (baseBranch) {
    try {
      await execFileAsync('git', ['fetch', 'origin', baseBranch], { cwd: workspace });
    } catch (err) {
      console.error(`[reset] step 5 (git fetch) failed: ${String(err)}`);
      return;
    }
    try {
      await execFileAsync('git', ['rebase', `origin/${baseBranch}`], { cwd: workspace });
    } catch (err) {
      console.error(`[reset] step 5 (git rebase) failed; aborting rebase: ${String(err)}`);
      try {
        await execFileAsync('git', ['rebase', '--abort'], { cwd: workspace });
      } catch (abortErr) {
        console.error(`[reset] step 5 (git rebase --abort) also failed: ${String(abortErr)}`);
      }
    }
  }
}
