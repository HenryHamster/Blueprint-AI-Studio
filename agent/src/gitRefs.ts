// Shared git remote-tracking-ref primitive.
//
// Lives in its own module (rather than on index.ts or workspacePrep.ts) so both
// workspace-preparation paths — prepareWorkspaceForRole (warm/cold role reuse)
// and setupWorkspace (the create/fix/refactor lane) — can use the SAME refspec
// form without importing each other and without either module growing a new
// public export that every vi.mock full-replacement factory would have to
// mirror.
//
// Dependency-free on purpose: it takes no logger and imports nothing from
// index.js, so it cannot participate in the existing index <-> workspacePrep
// import cycle. Callers own their own log format.
import { execFileSync } from 'child_process';

/**
 * Fetch `ref` from origin into `refs/remotes/origin/<ref>`.
 *
 * The EXPLICIT refspec is the whole point: `git clone --branch X` configures
 * `remote.origin.fetch` with only `+refs/heads/X:refs/remotes/origin/X`, so a
 * plain `git fetch origin <other-branch>` in that clone updates FETCH_HEAD and
 * never creates the remote-tracking ref. The `./check` gate derives its entire
 * work scope from `git diff origin/<base>...HEAD` and now FAILS CLOSED when
 * that ref is absent, so the tracking ref has to actually exist.
 *
 * Never throws — returns the failure so the caller can log it in its own
 * format. A failed fetch is non-fatal by design: it degrades to the gate's
 * loud fail-closed error rather than a silent pass.
 */
export function fetchRemoteTrackingRef(
  workspace: string,
  ref: string,
): { ok: true } | { ok: false; error: string } {
  try {
    execFileSync(
      'git',
      ['-C', workspace, 'fetch', 'origin', `${ref}:refs/remotes/origin/${ref}`],
      { encoding: 'utf-8', timeout: 120_000 },
    );
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/**
 * Integration branch the Blueprint self-repo's COMMITTED `./check` diffs
 * against (`BASE="automain"` in the tracked `check` at the repo root). Neither
 * a cold PR-branch clone nor `git clone --depth 100 --branch <task base>`
 * carries `origin/automain` or a local `automain`, so this ref has to be
 * fetched explicitly or the now fail-closed gate has nothing to compute a base
 * diff from. That bites hardest for subtasks whose base is a direction branch
 * (e.g. `research/<id>`), where the base is never the integration branch.
 *
 * This is a self-repo-specific constant, not a branch decision for arbitrary
 * projects: it is only ever consulted behind `isSelfRepo()`, and every
 * non-self-repo path resolves its base from the task (`resolveBaseBranch`).
 * Env-overridable for forks/tests, mirroring
 * prompts/code-change-helpers.ts BLUEPRINT_SELF_REPO. Registered in
 * SURFACE_CONFIGS['agent'] of backend/app/branch_literal_compliance.py so the
 * branch-literal guard sees it declared rather than dodged.
 *
 * Lives here, beside fetchRemoteTrackingRef, so BOTH workspace-preparation
 * entry points (workspacePrep.prepareWorkspaceForRole and index.setupWorkspace)
 * share one definition. It deliberately is NOT exported from workspacePrep.ts:
 * thirteen test files full-replace `vi.mock('../workspacePrep.js', () => ({}))`
 * and mock-factory-export-coverage.test.ts requires every such factory to
 * mirror each runtime export, so a new export there is a 13-file change.
 */
export function selfRepoIntegrationBranch(): string {
  // Read at call time (not module load) so it matches isSelfRepo's env handling.
  return process.env.BLUEPRINT_SELF_REPO_INTEGRATION_BRANCH || 'automain';
}

/** True when owner/repo identify the Blueprint self-repo. Env-overridable
 *  coordinates mirror prompts/code-change-helpers.ts BLUEPRINT_SELF_REPO; the
 *  committed `./check` on the self-repo diffs against selfRepoIntegrationBranch(),
 *  which neither a cold PR-branch clone nor a `--branch <base>` clone carries,
 *  so that ref must be fetched too (see fetchSelfRepoIntegrationRef callers). */
export function isSelfRepo(ownerRepo: { owner: string; repo: string } | null): boolean {
  if (!ownerRepo) return false;
  const selfOwner = (process.env.BLUEPRINT_SELF_REPO_OWNER || 'HenryHamster').toLowerCase();
  const selfRepo = (process.env.BLUEPRINT_SELF_REPO_NAME || 'BlueprintAIStudio').toLowerCase();
  return ownerRepo.owner.toLowerCase() === selfOwner && ownerRepo.repo.toLowerCase() === selfRepo;
}

/**
 * Fetch the self-repo integration branch into its remote-tracking ref when
 * (and only when) this workspace is the Blueprint self-repo and that ref is not
 * already among the branches the clone brought down.
 *
 * Shared by all THREE workspace-preparation entry points — the two
 * prepareWorkspaceForRole paths (warm/cold role reuse) and index.setupWorkspace
 * (the create/fix/refactor lane, which runs the TRACKED root `check` whose BASE
 * is the integration branch, NOT the task's base branch).
 *
 * Returns the ref that was fetched (for the caller to log), or null when the
 * fetch was correctly skipped. Never throws — a failed fetch reports itself
 * through `onError` and degrades to the ./check gate's loud fail-closed error.
 */
export function fetchSelfRepoIntegrationRef(
  workspace: string,
  ownerRepo: { owner: string; repo: string } | null,
  presentRefs: (string | undefined | null)[],
  onError: (ref: string, error: string) => void,
): string | null {
  if (!isSelfRepo(ownerRepo)) return null;
  const integrationRef = selfRepoIntegrationBranch();
  if (presentRefs.includes(integrationRef)) return null;
  const result = fetchRemoteTrackingRef(workspace, integrationRef);
  if (!result.ok) onError(integrationRef, result.error);
  return integrationRef;
}
