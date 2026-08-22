import type WebSocket from 'ws';

const CHECKPOINT_SUBJECT_PREFIX = 'blueprint-checkpoint: preserve unshipped task ';
const CHECKPOINT_VERSION = '1';

export interface TaskCheckpointMetadata {
  taskId: string;
  baseCommit: string;
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
}

export interface PrepareTaskWorkBranchDeps {
  git: (args: string[]) => string;
  listOpenPullRequestsForBranch: (
    ghToken: string,
    owner: string,
    repo: string,
    branch: string,
  ) => Promise<Array<{ number: number; html_url: string }>>;
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
): Promise<{ restoredCheckpoint: boolean }> {
  const { owner, repo, ghToken, baseBranch, workBranch, taskId, ws } = opts;
  const { git, listOpenPullRequestsForBranch, sendLog } = deps;

  let staleBranchExists = false;
  try {
    git(['ls-remote', '--exit-code', 'origin', workBranch]);
    staleBranchExists = true;
  } catch { /* branch does not exist on remote — normal first attempt */ }

  if (staleBranchExists) {
    const openPrs = await listOpenPullRequestsForBranch(ghToken, owner, repo, workBranch);
    if (openPrs.length > 0) {
      const prList = openPrs.map(p => `#${p.number}`).join(', ');
      throw new Error(
        `Refusing to delete stale branch ${workBranch}: has ${openPrs.length} open PR(s) (${prList}) ` +
        'from a previous attempt. Resolve the PR(s) manually before retrying.',
      );
    }

    // Materialize the exact remote tip before deciding whether this is disposable
    // stale state or the durable checkpoint written by code-change.ts.
    git([
      'fetch', '--no-tags', '--depth=100', 'origin',
      `+refs/heads/${workBranch}:refs/remotes/origin/${workBranch}`,
    ]);
    const remoteMessage = git(['show', '-s', '--format=%B', `origin/${workBranch}`]);
    const checkpoint = parseTaskCheckpointCommitMessage(remoteMessage, taskId);
    if (checkpoint) {
      git(['checkout', '-b', workBranch]);
      // A retry long after the checkpoint was written can leave the merge base
      // outside both shallow boundaries; without this the squash below fails
      // with "unrelated histories" on a clean tree and rethrows forever.
      ensureCheckpointMergeBase(git, baseBranch, workBranch, ws, sendLog);
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
      return { restoredCheckpoint: true };
    }

    // Legacy stale branches had no checkpoint provenance and retain the prior
    // cleanup behavior. Never interpret an arbitrary branch as retry work.
    try {
      git(['push', 'origin', '--delete', workBranch]);
      sendLog(ws, `Deleted stale remote branch ${workBranch} from previous attempt (no open PRs)\n`);
    } catch (error) {
      sendLog(
        ws,
        `WARNING: Could not delete stale remote branch ${workBranch}: ` +
        `${error instanceof Error ? error.message : String(error)}\n`,
      );
    }
  }

  git(['checkout', '-b', workBranch]);
  sendLog(ws, `Working branch: ${workBranch}\n`);
  return { restoredCheckpoint: false };
}
