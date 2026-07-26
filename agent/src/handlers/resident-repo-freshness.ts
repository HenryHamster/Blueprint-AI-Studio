import { execFileSync } from 'child_process';

const SHA_PATTERN = /^[0-9a-f]{7,64}$/i;
const MAX_COMMITS = 8;
const MAX_CHANGED_FILES = 40;
const GIT_TIMEOUT_MS = 120_000;

export interface ResidentRepoSnapshot {
  status: 'CURRENT' | 'UNKNOWN';
  head: string | null;
  previousHead: string | null;
  commits: string[];
  changedFiles: string[];
  omittedCommits: number;
  omittedChangedFiles: number;
  deltaAvailable: boolean;
  reason: string | null;
}

function git(workspace: string, args: string[], timeout = 30_000): string {
  return execFileSync('git', ['-C', workspace, ...args], {
    encoding: 'utf-8',
    timeout,
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

function boundedLines(raw: string, limit: number): { lines: string[]; omitted: number } {
  const lines = raw.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  return { lines: lines.slice(0, limit), omitted: Math.max(0, lines.length - limit) };
}

function boundedReason(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return raw.replace(/\s+/g, ' ').trim().slice(0, 240) || 'git synchronization failed';
}

/**
 * Refresh the read-only resident checkout immediately before a model turn.
 *
 * This deliberately does not watch or wake on repository changes. It only
 * synchronizes when the resident has already decided to run a turn.
 */
export function syncResidentRepository(
  workspace: string,
  baseBranch: string,
  previousDeliveredHead?: string | null,
): ResidentRepoSnapshot {
  const previousHead = typeof previousDeliveredHead === 'string'
    && SHA_PATTERN.test(previousDeliveredHead)
    ? previousDeliveredHead
    : null;
  try {
    git(workspace, ['fetch', 'origin', baseBranch], GIT_TIMEOUT_MS);
    git(workspace, ['reset', '--hard', `refs/remotes/origin/${baseBranch}`]);
    const head = git(workspace, ['rev-parse', 'HEAD']);
    if (!SHA_PATTERN.test(head)) throw new Error('git rev-parse returned an invalid HEAD');

    let deltaAvailable = previousHead !== null;
    let commitRaw = '';
    let changedRaw = '';
    if (previousHead && previousHead !== head) {
      try {
        // A rewritten/force-pushed branch makes the prior delivered SHA an
        // invalid baseline even when the object still exists locally. Freshness
        // is about the successfully synchronized current checkout; the optional
        // delta must never downgrade that truth to UNKNOWN.
        git(workspace, ['merge-base', '--is-ancestor', previousHead, head]);
        const range = `${previousHead}..${head}`;
        commitRaw = git(workspace, ['log', '--format=%h %s', range]);
        changedRaw = git(workspace, ['diff', '--name-only', range]);
      } catch {
        deltaAvailable = false;
      }
    }
    const commits = boundedLines(commitRaw, MAX_COMMITS);
    const changedFiles = boundedLines(changedRaw, MAX_CHANGED_FILES);
    return {
      status: 'CURRENT',
      head,
      previousHead,
      commits: commits.lines,
      changedFiles: changedFiles.lines,
      omittedCommits: commits.omitted,
      omittedChangedFiles: changedFiles.omitted,
      deltaAvailable,
      reason: null,
    };
  } catch (error) {
    let observedHead: string | null = null;
    try {
      const candidate = git(workspace, ['rev-parse', 'HEAD']);
      if (SHA_PATTERN.test(candidate)) observedHead = candidate;
    } catch {
      // The UNKNOWN state is sufficient; never hide the original sync failure.
    }
    return {
      status: 'UNKNOWN',
      head: observedHead,
      previousHead,
      commits: [],
      changedFiles: [],
      omittedCommits: 0,
      omittedChangedFiles: 0,
      deltaAvailable: false,
      reason: boundedReason(error),
    };
  }
}

export function formatResidentRepoSnapshot(snapshot: ResidentRepoSnapshot): string {
  const lines = [
    '## Repository freshness (code-owned, read-only)',
    `Status: ${snapshot.status}`,
    `Current HEAD: ${snapshot.head ?? 'unknown'}`,
    `Previously delivered HEAD: ${snapshot.previousHead ?? 'none'}`,
  ];
  if (snapshot.status === 'UNKNOWN') {
    lines.push(
      `Refresh error: ${snapshot.reason ?? 'unknown'}`,
      'Operator conversation remains available, but do not make repository-dependent claims or mutations from this checkout.',
    );
    return lines.join('\n');
  }
  if (!snapshot.deltaAvailable) {
    lines.push('Delta: unavailable; baseline reset to the current HEAD.');
    return lines.join('\n');
  }
  if (snapshot.commits.length > 0) {
    lines.push('Commits since the previously delivered HEAD:', ...snapshot.commits.map(v => `- ${v}`));
    if (snapshot.omittedCommits > 0) lines.push(`- ... ${snapshot.omittedCommits} more commit(s) omitted`);
  } else {
    lines.push('Commits since the previously delivered HEAD: none');
  }
  if (snapshot.changedFiles.length > 0) {
    lines.push('Changed files:', ...snapshot.changedFiles.map(v => `- ${v}`));
    if (snapshot.omittedChangedFiles > 0) {
      lines.push(`- ... ${snapshot.omittedChangedFiles} more file(s) omitted`);
    }
  }
  return lines.join('\n');
}
