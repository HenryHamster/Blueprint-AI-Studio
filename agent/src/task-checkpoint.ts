import type WebSocket from 'ws';

const CHECKPOINT_SUBJECT_PREFIX = 'blueprint-checkpoint: preserve unshipped task ';
const CHECKPOINT_VERSION = '1';

export interface TaskCheckpointMetadata {
  taskId: string;
  baseCommit: string;
}

export interface TaskOwnedOpenPullRequest {
  number: number;
  branch: string;
}

/** Read the backend-persisted PR linkage from a task's canonical first payload. */
export function taskOwnedOpenPullRequest(
  payload: Record<string, unknown>,
): TaskOwnedOpenPullRequest | undefined {
  const rawNumber = payload.pr_number;
  const number = typeof rawNumber === 'number'
    ? rawNumber
    : (typeof rawNumber === 'string' && /^\d+$/.test(rawNumber) ? Number(rawNumber) : NaN);
  const branch = typeof payload.branch === 'string' ? payload.branch : '';
  if (!Number.isSafeInteger(number) || number <= 0 || !branch) return undefined;
  return { number, branch };
}

export function buildTaskCheckpointCommitMessage(taskId: string, baseCommit: string): string {
  return `${CHECKPOINT_SUBJECT_PREFIX}${taskId}\n\n` +
    `Blueprint-Checkpoint-Version: ${CHECKPOINT_VERSION}\n` +
    `Blueprint-Task-Id: ${taskId}\n` +
    `Blueprint-Base-Commit: ${baseCommit}`;
}

export function parseTaskCheckpointCommitMessage(
  message: string,
  expectedTaskId: string,
): TaskCheckpointMetadata | null {
  const lines = message.replace(/\r\n/g, '\n').split('\n');
  if (lines[0] !== `${CHECKPOINT_SUBJECT_PREFIX}${expectedTaskId}`) return null;

  const trailers = new Map<string, string>();
  for (const line of lines.slice(1)) {
    const match = /^([A-Za-z][A-Za-z-]*):\s*(.*)$/.exec(line);
    if (match) trailers.set(match[1], match[2]);
  }
  const taskId = trailers.get('Blueprint-Task-Id');
  const baseCommit = trailers.get('Blueprint-Base-Commit');
  if (
    trailers.get('Blueprint-Checkpoint-Version') !== CHECKPOINT_VERSION ||
    taskId !== expectedTaskId ||
    !baseCommit ||
    !/^[0-9a-f]{7,64}$/i.test(baseCommit)
  ) {
    return null;
  }
  return { taskId, baseCommit };
}

/**
 * Deepen a shallow clone on demand so a checkpoint squash-restore can find its
 * merge base.
 *
 * Task workspaces are cloned shallow (``DEFAULT_CLONE_DEPTH``) and the
 * checkpoint ref below is fetched at ``--depth=100``. When a task is retried
 * long after its checkpoint was written, the true merge base can sit outside
 * BOTH shallow boundaries — the observed production case was a merge base 213
 * commits behind ``main``. Git then reports
 * ``fatal: refusing to merge unrelated histories`` and, because it refuses
 * before touching the index, the worktree stays clean: the conflict-preserving
 * branch in ``prepareTaskWorkBranch`` sees no ``status`` output and rethrows.
 * Every retry reproduces it byte for byte, so the task dies permanently with
 * its unshipped work still sitting on the remote.
 *
 * ``audits.ts``, ``audit-challenge.ts`` and ``merge-grade.ts`` already clone at
 * depth 0 for exactly this long-diverged case. Deepening on demand is the same
 * defense applied lazily, so the common recent-checkpoint retry keeps paying
 * only the cheap shallow fetch.
 *
 * Deliberately probe by exit status rather than by output: ``git merge-base``
 * exits non-zero when no base is reachable, which is the only signal that
 * distinguishes "outside the shallow window" from "genuinely unrelated". If a
 * base is still unreachable after unshallowing the histories really are
 * disjoint, and refusing is correct — a squash across unrelated trees would
 * fabricate a diff. The remote checkpoint is left intact either way.
 */
function ensureCheckpointMergeBase(
  git: (args: string[]) => string,
  baseBranch: string,
  workBranch: string,
  ws: WebSocket | null,
  sendLog: (ws: WebSocket | null, content: string) => void,
): void {
  let shallow = false;
  try {
    shallow = git(['rev-parse', '--is-shallow-repository']).trim() === 'true';
  } catch { /* older git or non-repo probe failure — nothing to deepen */ }
  if (!shallow) return;

  try {
    git(['merge-base', 'HEAD', `origin/${workBranch}`]);
    return;
  } catch { /* merge base is outside the shallow window — deepen below */ }

  sendLog(
    ws,
    `Checkpoint merge base for ${workBranch} is outside the shallow clone window; ` +
    'fetching full history before restoring.\n',
  );
  git(['fetch', '--no-tags', '--unshallow', 'origin']);
  git([
    'fetch', '--no-tags', 'origin',
    `+refs/heads/${workBranch}:refs/remotes/origin/${workBranch}`,
  ]);

  try {
    git(['merge-base', 'HEAD', `origin/${workBranch}`]);
  } catch {
    throw new Error(
      `Checkpoint branch ${workBranch} shares no history with ${baseBranch} even after ` +
      'fetching full history; refusing to squash unrelated trees. The remote checkpoint ' +
      'is left intact for manual recovery.',
    );
  }
}

export interface PrepareTaskWorkBranchOpts {
  owner: string;
  repo: string;
  ghToken: string;
  baseBranch: string;
  workBranch: string;
  taskId: string;
  ws: WebSocket | null;
  /** Canonical task-row linkage written when this task opened its PR. */
  ownedOpenPullRequest?: TaskOwnedOpenPullRequest;
}

export interface OpenPullRequestBranchState {
  number: number;
  html_url: string;
  head?: { ref?: string; sha?: string; repo?: { full_name?: string } | null };
  base?: { ref?: string };
}

export interface PrepareTaskWorkBranchDeps {
  git: (args: string[]) => string;
  listOpenPullRequestsForBranch: (
    ghToken: string,
    owner: string,
    repo: string,
    branch: string,
  ) => Promise<OpenPullRequestBranchState[]>;
  sendLog: (ws: WebSocket | null, content: string) => void;
}

/**
 * Create a task work branch, restoring a durable unshipped-work checkpoint when
 * the previous attempt reached the fail-closed path before opening a PR.
 *
 * A checkpoint is restored as a squash merge into the freshly cloned base. This
 * deliberately leaves the recovered diff uncommitted: callers capture baseCommit
 * after this function, so the normal ./pr and changed-files gates continue to see
 * the complete recovered change rather than treating the checkpoint as the base.
 * The remote checkpoint stays in place until ./pr replaces it, closing the
 * clone-to-next-checkpoint crash window on a fresh retry VM.
 */
export async function prepareTaskWorkBranch(
  opts: PrepareTaskWorkBranchOpts,
  deps: PrepareTaskWorkBranchDeps,
): Promise<{
  restoredCheckpoint: boolean;
  resumedOpenPullRequest?: { number: number; htmlUrl: string };
}> {
  const { owner, repo, ghToken, baseBranch, workBranch, taskId, ws } = opts;
  const { git, listOpenPullRequestsForBranch, sendLog } = deps;

  let staleBranchTip = '';
  try {
    staleBranchTip = git(['ls-remote', '--exit-code', 'origin', workBranch])
      .trim().split(/\s+/)[0] ?? '';
  } catch (error) {
    // `git ls-remote --exit-code` uses status 2 only for "no matching ref".
    // Authentication, transport, timeout, and protocol failures must not be
    // reinterpreted as branch absence: doing so loses the authority read and
    // can create or overwrite work under an ambiguous remote state.
    const status = typeof error === 'object' && error !== null && 'status' in error
      ? Number((error as { status?: unknown }).status)
      : NaN;
    if (status !== 2) {
      throw new Error(
        `Cannot determine whether task branch ${workBranch} exists; refusing retry without ` +
        `an authoritative remote read: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
    // Status 2 is the normal first-attempt case: the branch does not exist.
  }
  if (staleBranchTip && !/^[0-9a-f]{40,64}$/i.test(staleBranchTip)) {
    throw new Error(
      `Cannot determine exact head of task branch ${workBranch}; refusing malformed remote read.`,
    );
  }

  if (staleBranchTip) {
    const openPrs = await listOpenPullRequestsForBranch(ghToken, owner, repo, workBranch);
    if (openPrs.length > 0) {
      const prList = openPrs.map(p => `#${p.number}`).join(', ');
      if (openPrs.length !== 1) {
        throw new Error(
          `Cannot resume task branch ${workBranch}: has ${openPrs.length} open PR(s) (${prList}); ` +
          'the retry authority is ambiguous.',
        );
      }

      // The branch name is derived from the exact task id, so its one open PR is
      // this task's normal in-place repair surface only when every immutable
      // identity agrees.  A branch/API race, forked head, or retargeted base must
      // fail closed rather than checking out or overwriting an ambiguous writer.
      const pr = openPrs[0];
      const expectedRepo = `${owner}/${repo}`.toLowerCase();
      const expectedUrl = `https://github.com/${owner}/${repo}/pull/${pr.number}`;
      const headSha = pr.head?.sha ?? '';
      const identitiesMatch = (
        opts.ownedOpenPullRequest?.number === pr.number &&
        opts.ownedOpenPullRequest?.branch === workBranch &&
        pr.head?.ref === workBranch &&
        pr.base?.ref === baseBranch &&
        pr.head?.repo?.full_name?.toLowerCase() === expectedRepo &&
        pr.html_url === expectedUrl &&
        /^[0-9a-f]{40,64}$/i.test(headSha) &&
        headSha === staleBranchTip
      );
      if (!identitiesMatch) {
        throw new Error(
          `Cannot resume task branch ${workBranch}: open PR #${pr.number} cannot be ` +
          'proven to match the exact task branch, repository, base, and observed head SHA.',
        );
      }

      git([
        'fetch', '--no-tags', '--depth=100', 'origin',
        `+refs/heads/${workBranch}:refs/remotes/origin/${workBranch}`,
      ]);
      const fetchedSha = git(['rev-parse', `origin/${workBranch}`]).trim();
      if (fetchedSha !== headSha) {
        throw new Error(
          `Open PR #${pr.number} head changed while preparing retry for ${workBranch}; ` +
          'refusing to resume from an ambiguous branch tip.',
        );
      }
      git(['checkout', '-b', workBranch, '--track', `origin/${workBranch}`]);
      // Carry the exact retry authority through the model/tool turn.  Reading a
      // fresh remote head only at ./pr time would let a concurrent writer advance
      // this branch after checkout and then be silently accepted as the new
      // force-with-lease target.  The generated helper advances this local lease
      // only after a successful or exactly reconciled push.
      git(['config', 'blueprint.taskRemoteLeaseSha', headSha]);
      sendLog(
        ws,
        `Resuming open PR #${pr.number} for task ${taskId} at exact head ${headSha}; ` +
        'the retry will update the existing task branch in place.\n',
      );
      return {
        restoredCheckpoint: false,
        resumedOpenPullRequest: { number: pr.number, htmlUrl: pr.html_url },
      };
    }

    // Materialize the exact remote tip before deciding whether this is disposable
    // stale state or the durable checkpoint written by code-change.ts.
    git([
      'fetch', '--no-tags', '--depth=100', 'origin',
      `+refs/heads/${workBranch}:refs/remotes/origin/${workBranch}`,
    ]);
    const fetchedSha = git(['rev-parse', `origin/${workBranch}`]).trim();
    if (fetchedSha !== staleBranchTip) {
      throw new Error(
        `Task branch ${workBranch} advanced while checking for retry ownership; ` +
        'refusing stale cleanup or checkpoint restore from an ambiguous tip.',
      );
    }
    const remoteMessage = git(['show', '-s', '--format=%B', `origin/${workBranch}`]);
    const checkpoint = parseTaskCheckpointCommitMessage(remoteMessage, taskId);
    if (checkpoint) {
      git(['checkout', '-b', workBranch]);
      // A retry long after the checkpoint was written can leave the merge base
      // outside both shallow boundaries; without this the squash below fails
      // with "unrelated histories" on a clean tree and rethrows forever.
      ensureCheckpointMergeBase(git, baseBranch, workBranch, ws, sendLog);
      // The deepening helper may have to refetch this branch. Re-bind that local
      // remote-tracking ref to the exact checkpoint tip already parsed above
      // before allowing any squash merge from it.
      const restoreTip = git(['rev-parse', `origin/${workBranch}`]).trim();
      if (restoreTip !== fetchedSha) {
        throw new Error(
          `Checkpoint branch ${workBranch} advanced while deepening its history; ` +
          'refusing to restore an unverified tip.',
        );
      }
      try {
        git(['merge', '--squash', '--no-commit', `origin/${workBranch}`]);
        sendLog(
          ws,
          `Restored durable unshipped-work checkpoint for task ${taskId} from ${workBranch}; ` +
          'recovered changes remain uncommitted for the retry.\n',
        );
      } catch (error) {
        // A base-branch advance can make the squash restore conflict. Keep the
        // conflict in the worktree so the retrying code agent can resolve it;
        // importantly, the remote checkpoint remains intact as counter-proof.
        const status = git(['status', '--porcelain']);
        if (!status) throw error;
        sendLog(
          ws,
          `Checkpoint restore for task ${taskId} has merge conflicts after ${baseBranch} advanced; ` +
          'preserving the conflicted recovery tree for the retry to resolve.\n',
        );
      }
      // The checkpoint tip is the same exact publication authority as an open
      // PR head. Carry it across the model/tool turn so a later remote advance
      // cannot be freshly adopted as the force-with-lease target.
      git(['config', 'blueprint.taskRemoteLeaseSha', fetchedSha]);
      return { restoredCheckpoint: true };
    }

    // There is no atomic Git primitive that can prove both "tip is unchanged"
    // and "no PR was opened" while deleting a branch.  The legacy automatic
    // delete could therefore remove a concurrent writer or a just-opened PR.
    // Preserve the branch and require an explicit, separately reconciled cleanup.
    throw new Error(
      `Remote branch ${workBranch} exists without this task's checkpoint or an ` +
      'authoritatively linked open PR; refusing automatic deletion.',
    );
  }

  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);
  return { restoredCheckpoint: false };
}
