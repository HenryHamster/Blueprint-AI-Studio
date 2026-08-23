/**
 * Authoritative PR merge state for the review path.
 *
 * The reviewer used to be given no merge signal at all while the verdict
 * contract listed "a merge conflict" as a REQUEST_CHANGES ground, so it
 * improvised a merge simulation. On rollup PR #17720 it ran the legacy
 * 3-argument `git merge-tree` against a base that was not the merge base,
 * which fabricates conflict markers, and minted a MAJOR finding no fix agent
 * could ever close. The chain plateaued at two unresolved findings for four
 * rounds and parked for a human.
 *
 * GitHub's `mergeable` is the same computation that performs the merge, so it
 * is the authority. Fetch it here and let the prompt state it as a fact.
 *
 * Extracted into its own module rather than living in review-fix.ts purely to
 * stay under that file's size cap.
 */
import { execFileSync } from 'child_process';
import type WebSocket from 'ws';
import { sendLog } from '../index.js';
import type { ReviewMergeState } from '../prompts/review-prompt.js';

/**
 * GitHub computes `mergeable` lazily: the first read of a cold PR returns
 * `null` while a background job runs. The fix path tolerates that null; the
 * review path must not, because a null silently degrades the reviewer to
 * "merge state unknown" — the very state that licenses guessing. One bounded
 * re-poll converts most nulls into a real answer for a couple of seconds.
 */
export const MERGE_STATE_REPOLL_DELAY_MS = 2_000;

/**
 * Read GitHub's authoritative merge state for a PR. Never throws: a review has
 * to run even when GitHub is unreachable, so every failure degrades to
 * `{ fetched: false }` and the prompt says the state is unknown.
 */
export async function fetchAuthoritativeMergeState(opts: {
  ghToken: string;
  owner: string;
  repo: string;
  prNumber: number;
  ws: WebSocket | null;
  /** Overridable only so tests do not pay the real re-poll wait. */
  repollDelayMs?: number;
}): Promise<ReviewMergeState> {
  const { ghToken, owner, repo, prNumber, ws } = opts;
  const readOnce = (): ReviewMergeState => {
    const prJson = execFileSync('curl', [
      '-sS', '--fail-with-body', '--max-time', '20',
      '-H', `Authorization: Bearer ${ghToken}`,
      '-H', 'Accept: application/vnd.github+json',
      `https://api.github.com/repos/${owner}/${repo}/pulls/${prNumber}`,
    ], { encoding: 'utf-8', timeout: 25_000 });
    const prObj = JSON.parse(prJson) as {
      mergeable?: boolean | null;
      mergeable_state?: string;
      base?: { ref?: string };
      head?: { sha?: string };
    };
    return {
      mergeable: prObj.mergeable,
      mergeStateStatus: prObj.mergeable_state,
      baseRef: prObj.base?.ref,
      headSha: prObj.head?.sha,
      fetched: true,
    };
  };
  try {
    let state = readOnce();
    if (state.mergeable === null) {
      const delay = opts.repollDelayMs ?? MERGE_STATE_REPOLL_DELAY_MS;
      await new Promise(resolve => setTimeout(resolve, delay));
      state = readOnce();
    }
    sendLog(ws, `PR #${prNumber} authoritative merge state: mergeable=${String(state.mergeable)} mergeable_state=${state.mergeStateStatus ?? 'unknown'}\n`);
    return state;
  } catch (e) {
    sendLog(ws, `Could not establish PR #${prNumber} merge state; the review will proceed with it unknown: ${e instanceof Error ? e.message : String(e)}\n`);
    return { mergeable: undefined, fetched: false };
  }
}
