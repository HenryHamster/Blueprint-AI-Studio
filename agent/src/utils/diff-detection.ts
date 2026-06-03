/**
 * Diff analysis and loop divergence detection for the fix-loop.
 *
 * Detects when consecutive fix-loop iterations make no measurable progress
 * (e.g., the diff reverts to a prior state) using Jaccard token-set similarity
 * and line-level diff capture. Used to signal convergence failures and prevent
 * infinite retry loops.
 */

import { execSync } from 'child_process';
import { FIX_LOOP_JACCARD_THRESHOLD, FIX_LOOP_DIFF_REVERT_THRESHOLD } from '../constants.js';

const WORKSPACE = process.env.AGENT_WORKSPACE || '/tmp/workspace';

const _TOKEN_RE = /\w+/g;

/** Jaccard token-set similarity between two strings. Range [0,1]. */
export function jaccardSimilarity(a: string, b: string): number {
  const tokA = new Set(a.toLowerCase().match(_TOKEN_RE) ?? []);
  const tokB = new Set(b.toLowerCase().match(_TOKEN_RE) ?? []);
  if (tokA.size === 0 && tokB.size === 0) return 1;
  if (tokA.size === 0 || tokB.size === 0) return 0;
  let intersection = 0;
  for (const t of tokA) if (tokB.has(t)) intersection++;
  return intersection / (tokA.size + tokB.size - intersection);
}

export interface DiffLines { added: Set<string>; removed: Set<string> }

/** Capture the set of added/removed lines in the working tree vs HEAD. */
export function captureDiffLines(): DiffLines {
  const added = new Set<string>();
  const removed = new Set<string>();
  try {
    const raw = execSync('git diff HEAD --no-color', { cwd: WORKSPACE, timeout: 10_000 }).toString();
    let count = 0;
    for (const line of raw.split('\n')) {
      if (++count > 5000) break;
      const content = line.slice(1).trim();
      if (!content) continue;
      if (line.startsWith('+') && !line.startsWith('+++')) added.add(content);
      else if (line.startsWith('-') && !line.startsWith('---')) removed.add(content);
    }
  } catch { /* git unavailable or clean tree */ }
  return { added, removed };
}

/**
 * True when currDiff substantially undoes prevDiff — meaning that a large
 * fraction of prevDiff's added lines have vanished in currDiff.
 */
function detectDiffRevert(prev: DiffLines, curr: DiffLines, threshold: number): boolean {
  if (prev.added.size === 0) return false;
  let vanished = 0;
  for (const line of prev.added) if (!curr.added.has(line)) vanished++;
  return vanished / prev.added.size >= threshold;
}

export type DivergenceReason = 'divergence_oscillation' | 'divergence_similar_error' | 'divergence_diff_revert';

/**
 * Check for three divergence patterns after at least 3 fix iterations.
 * Returns the first detected reason, or null if none found.
 * Conservative: does not fire until fingerprintHistory has ≥ 3 entries.
 */
export function detectLoopDivergence(
  fingerprintHistory: string[],
  errorTextHistory: string[],
  prevDiff: DiffLines | null,
  currDiff: DiffLines | null,
): DivergenceReason | null {
  if (fingerprintHistory.length < 3) return null;
  const N = fingerprintHistory.length - 1;

  // Oscillation: error A -> error B -> error A (fingerprint repeats at N-2)
  if (fingerprintHistory[N] === fingerprintHistory[N - 2] && fingerprintHistory[N - 1] !== fingerprintHistory[N]) {
    return 'divergence_oscillation';
  }

  // Jaccard: both consecutive error pairs are semantically near-identical
  const j1 = jaccardSimilarity(errorTextHistory[N], errorTextHistory[N - 1]);
  const j2 = jaccardSimilarity(errorTextHistory[N - 1], errorTextHistory[N - 2]);
  if (j1 >= FIX_LOOP_JACCARD_THRESHOLD && j2 >= FIX_LOOP_JACCARD_THRESHOLD) {
    return 'divergence_similar_error';
  }

  // Diff revert: last Claude iteration substantially undid the previous one
  if (prevDiff !== null && currDiff !== null && detectDiffRevert(prevDiff, currDiff, FIX_LOOP_DIFF_REVERT_THRESHOLD)) {
    return 'divergence_diff_revert';
  }

  return null;
}
