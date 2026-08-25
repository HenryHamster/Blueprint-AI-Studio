/**
 * Resolve derived-artifact merge conflicts without spending a Claude turn.
 *
 * THE DEFECT
 * ----------
 * Two committed derived artifacts re-conflict every open PR whenever anything
 * merges into `automain`: `test-estate-ranked-queue.md` (a ~1200-line generated
 * report whose ranks shift table-wide when any test file is added) and
 * `backend/.test_durations` (a ~21000-line sorted JSON map where both sides only
 * ever INSERT keys, yet appending rewrites the previous last line to add a comma,
 * so git still reports a textual conflict).
 *
 * The pipeline's answer to a conflict is expensive. `orchestrator_ci_monitor.py`
 * sees the PR go unmergeable and spawns a `fix` task — a provisioned VM plus a
 * Claude turn (`review_loop.conflict_fix_spawned`). The fix handler then rebases,
 * aborts on failure, and re-merges SPECIFICALLY so that Claude can see conflict
 * markers. So a conflict whose correct resolution is 100% mechanical burns an LLM
 * turn, force-pushes, and restarts CI (5–8 minutes a round). On 2026-08-25,
 * merging #17939, #17952 and #17955 did this three times in a row.
 *
 * WHY THIS IS NEEDED EVEN THOUGH A MERGE DRIVER EXISTS
 * ----------------------------------------------------
 * `.gitattributes` now maps `backend/.test_durations` to the `testdurations`
 * merge driver, which stops that conflict materializing during a LOCAL rebase.
 * That is not sufficient on its own, for two independent reasons:
 *
 *  - GitHub computes `mergeable` server-side with stock drivers and never
 *    consults ours, so the PR still goes unmergeable and the fix task is still
 *    spawned. Something has to end the turn cheaply once it exists.
 *  - `test-estate-ranked-queue.md` cannot be a merge driver at all. A driver is
 *    handed three blobs, and the one correct resolution is a function of the
 *    whole MERGED TREE, which does not exist yet at driver time.
 *
 * WHAT IT REFUSES TO DO
 * ---------------------
 * The policy and the refusals live in `scripts/derived_artifact_merge_resolve.py`,
 * deliberately: the resolver is version-controlled beside the artifacts it knows
 * how to merge, is unit-tested in the scripts lane, and is the same command a
 * human runs to sync a branch by hand. This module only drives it and reports.
 * It treats any exit code other than 0 as "hand this to Claude", so a conflict
 * the resolver declines — an unregistered path, a re-timed measurement — takes
 * exactly the path it takes today.
 *
 * It completes the task without Claude ONLY when conflict resolution was the
 * task's entire job (`isConflictOnlyFixTask`). That guard is load-bearing and is
 * the same one the stale-premise obsolete-exit uses: regression 2026-08-20, fix
 * task 10384f8e was scoped to three BP- review issue ids, hit a clean PR, and was
 * voided with all three findings untouched. Voiding a task discards work with no
 * trace, so a fix that also carries review findings runs Claude as usual — it
 * just no longer has to hand-resolve the derived files first.
 */
import { execFileSync } from 'child_process';

/** Repo-relative path to the resolver, run from the workspace root. */
export const DERIVED_RESOLVER_SCRIPT = 'scripts/derived_artifact_merge_resolve.py';

/** Bounded: the resolver regenerates the ranked queue, which takes ~15s on this repo. */
export const DERIVED_RESOLVE_TIMEOUT_MS = 180_000;

/**
 * Structured spawn reasons whose ENTIRE job is the rebase.
 *
 * `fix_reason` is the backend's structured spawn provenance
 * (review_pipeline.py `_spawn_pipeline_task`). Prose matching on the prompt
 * cannot carry this weight: a prompt merely REFERRING to a conflict ("an
 * operator resolved the merge conflict") would match it.
 */
export const CONFLICT_ONLY_FIX_REASONS = new Set(['merge_conflict', 'update_branch_failed']);

/** Shape of `--json` from scripts/derived_artifact_merge_resolve.py. */
export interface DerivedResolveReport {
  resolved: string[];
  unhandled: string[];
  details: Record<string, string>;
  error: string | null;
  complete: boolean;
}

export interface DerivedResolveOutcome {
  /** True only when every conflict was resolved, committed and pushed. */
  complete: boolean;
  /** Paths the resolver merged mechanically. */
  resolved: string[];
  /** Conflicts it declined; non-empty means Claude must handle the merge. */
  unhandled: string[];
  /** One-line human summary for the task log and the completion status. */
  summary: string;
}

/**
 * True when this fix task exists ONLY to resolve a conflict.
 *
 * Absent a `fix_reason`, assume the task has real work: running Claude on an
 * already-clean PR wastes a little spend, whereas voiding a task discards work
 * with no trace. A prompt naming BP- review issue ids always has real work.
 */
export function isConflictOnlyFixTask(fixReason: string | undefined, prompt: string): boolean {
  return CONFLICT_ONLY_FIX_REASONS.has(fixReason ?? '') && !/\bBP-[0-9a-f]{12}\b/.test(prompt);
}

/**
 * Parse the resolver's `--json` report, or null when it is not usable.
 *
 * Fail-closed: an unparseable report, or one missing the fields the caller
 * keys off, must never be read as a completed resolution. Null routes the
 * merge to Claude exactly as if the resolver had declined.
 */
export function parseResolverReport(stdout: string): DerivedResolveReport | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  const report = parsed as Partial<DerivedResolveReport>;
  if (typeof report.complete !== 'boolean') return null;
  if (!Array.isArray(report.resolved) || !Array.isArray(report.unhandled)) return null;
  return {
    resolved: report.resolved.map(String),
    unhandled: report.unhandled.map(String),
    details: (report.details && typeof report.details === 'object' ? report.details : {}) as Record<string, string>,
    error: typeof report.error === 'string' ? report.error : null,
    complete: report.complete,
  };
}

/** Build the one-line summary that lands in the task log and the PR trail. */
export function describeResolution(report: DerivedResolveReport): string {
  const what = report.resolved
    .map(path => `${path} (${report.details[path] ?? 'resolved'})`)
    .join('; ');
  return `Resolved ${report.resolved.length} derived-artifact merge conflict(s) mechanically, with no agent turn: ${what}`;
}

/**
 * Try to finish an in-progress merge mechanically, then commit and push it.
 *
 * Returns `complete: false` for every path that must reach Claude, including
 * every failure mode: the resolver missing, a non-zero exit, an unparseable
 * report, or a commit/push that did not land. A resolution that is not pushed
 * is worse than none at all — GitHub would still report the PR unmergeable and
 * the orchestrator would respawn the same fix forever — so the push is part of
 * the success condition, not a follow-up.
 *
 * `push` and `log` are injected rather than imported so this module does not
 * have to import back from review-fix.ts or index.ts (a cycle), and so tests can
 * drive the commit/push sequencing without a remote or a live socket.
 */
export async function resolveDerivedArtifactConflicts(opts: {
  workspace: string;
  branch: string;
  log: (line: string) => void;
  push: (branch: string) => Promise<void>;
}): Promise<DerivedResolveOutcome> {
  const { workspace, branch, log, push } = opts;
  const decline = (summary: string, unhandled: string[] = []): DerivedResolveOutcome => {
    log(`${summary}\n`);
    return { complete: false, resolved: [], unhandled, summary };
  };

  let stdout: string;
  try {
    stdout = execFileSync(
      'python3',
      [DERIVED_RESOLVER_SCRIPT, '--repo', workspace, '--json'],
      { cwd: workspace, encoding: 'utf-8', timeout: DERIVED_RESOLVE_TIMEOUT_MS },
    );
  } catch (e) {
    // Exit 1 (a conflict it declined) and exit 2 (nothing to resolve) both land
    // here, as does a missing interpreter. All of them mean: let Claude do it.
    const err = e as { stdout?: string; message?: string };
    const report = parseResolverReport(err.stdout ?? '');
    const why = report?.unhandled?.length
      ? `conflicts outside the derived artifacts: ${report.unhandled.join(', ')}`
      : (err.message ?? String(e)).slice(0, 300);
    return decline(
      `Mechanical derived-artifact resolution declined — Claude will resolve them (${why})`,
      report?.unhandled ?? [],
    );
  }

  const report = parseResolverReport(stdout);
  if (!report || !report.complete) {
    return decline(
      'Mechanical derived-artifact resolution produced no usable report — Claude will resolve the conflicts',
      report?.unhandled ?? [],
    );
  }

  const summary = describeResolution(report);
  log(`${summary}\n`);
  try {
    execFileSync('git', ['-C', workspace, 'commit', '--no-edit'], {
      encoding: 'utf-8',
      timeout: 60_000,
    });
  } catch (e) {
    return decline(
      `Derived-artifact conflicts resolved but the merge commit failed — Claude will take over: ${String(e).slice(0, 300)}`,
    );
  }
  try {
    await push(branch);
  } catch (e) {
    // The merge is committed locally but the branch on GitHub is unchanged, so
    // the PR is still unmergeable. Report incomplete so the caller runs Claude
    // rather than completing a task whose result never left the VM.
    return decline(
      `Derived-artifact conflicts resolved but the push failed — Claude will take over: ${String(e).slice(0, 300)}`,
    );
  }
  log(`Pushed the mechanically resolved merge to ${branch}.\n`);
  return { complete: true, resolved: report.resolved, unhandled: [], summary };
}
