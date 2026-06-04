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
//   2. agent/src/index.ts:2102  — `buildBaseCliEnv()` sets
//      `HOME: process.env.HOME || '/home/agent'` for the subprocess; that
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

// /tmp scratch-file names mirror agent/src/index.ts ~line 2809 (the
// shutdown cleanup loop). Keep them in sync if either list grows.
const TMP_OUTPUT_NAMES = [
  'review_verdict',
  'panel_review_verdict',
  'plan_output',
  'research_findings',
  'security_findings',
  'smoketest_result',
  'debug_output',
  'perf_output',
  'investigation_report',
  'strategic_output',
  'design_doc_output',
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

// Home-relative provider credential / CLI-state paths the boot manifest
// materializes (build_agent_secret_manifest → materialize_manifest.py): provider
// API/OAuth cred dirs plus the Claude Code onboarding-bypass file ~/.claude.json
// and ~/.gemini/settings.json. These are ACCOUNT-SCOPED, not per-task, so they
// must be wiped ONLY when the next role binds a DIFFERENT account — and on that
// account-divergent path performWarmRetarget (index.ts) clears them via
// clearProviderCredentials and immediately re-materializes the new account's
// files before the role runs, so there is no leakage and no missing-config gap.
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

// systemd EnvironmentFile written at boot. Root-owned 0600, so the agent user
// usually needs sudo to remove it; clearProviderCredentials falls back to
// `sudo rm` on EACCES/EPERM. Tests pass a writable temp path so neither sudo
// nor the real /etc file is touched.
const SYSTEM_ENV_FILE = '/etc/agent.env';

/** Remove a path, falling back to `sudo rm -rf` when the agent user lacks
 *  direct permission (e.g. the root-owned /etc/agent.env). All failures are
 *  swallowed (logged) — credential clearing is best-effort and must never
 *  abort the between-role reset. */
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
        console.error(`[reset] clearProviderCredentials sudo rm ${target}: ${String(sudoErr)}`);
        return;
      }
    }
    console.error(`[reset] clearProviderCredentials rm ${target}: ${String(err)}`);
  }
}

/**
 * Clear the prior account's credential material so it cannot leak into the
 * next role. Wipes the systemd env file (provider/auth/model + secret env) and
 * the provider CLI credential/state directories under the agent's home.
 *
 * Exported so the warm re-target path (index.ts performWarmRetarget) can call it
 * explicitly before materializing a new account's manifest — guaranteeing
 * clear-then-write ordering on the account-divergent path. It is deliberately
 * NOT called from resetForNextRole's same-account fast path (see the
 * PROVIDER_CRED_HOME_PATHS comment): there the boot-materialized config is still
 * correct and nothing would re-create it.
 */
export async function clearProviderCredentials(
  homeDir: string = process.env.HOME || os.homedir(),
  envFilePath: string = SYSTEM_ENV_FILE,
): Promise<void> {
  await rmBestEffort(envFilePath);
  for (const rel of PROVIDER_CRED_HOME_PATHS) {
    await rmBestEffort(path.join(homeDir, rel));
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
  } catch (err) {
    console.error(`[reset] step 2 (/tmp cleanup) failed: ${String(err)}`);
  }

  // Step 3 — wipe Claude Code session state at `~/.claude/`. See spike
  // comment at top of file for path provenance.
  //
  // The wipe target MUST match the HOME the spawned Claude Code subprocess
  // sees (index.ts:buildBaseCliEnv → `HOME: process.env.HOME || '/home/agent'`).
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
  // claims instead clear-then-re-materialize via performWarmRetarget
  // (index.ts → clearProviderCredentials) before the next role runs, so there
  // is no cross-account leakage on the only path where a leak is possible.

  // Step 4 — reset the workspace to a clean HEAD while preserving
  // dependency caches. This is the only step we re-throw on; if the
  // workspace itself is gone we cannot hand off to the next role.
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
