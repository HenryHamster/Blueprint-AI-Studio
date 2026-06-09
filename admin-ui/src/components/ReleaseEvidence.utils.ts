import { type LiveReleaseEvidence } from '../api';

/** First 8 chars of a promoted SHA, or null when absent. */
export function shortPromotedSha(sha?: string | null): string | null {
  if (typeof sha !== 'string' || !sha) return null;
  return sha.slice(0, 8);
}

/** serving_confirmed → green, any other status → amber. */
export function servingStatusClass(status?: string | null): string {
  return status === 'serving_confirmed'
    ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-300'
    : 'bg-amber-500/10 border-amber-500/20 text-amber-300';
}

/**
 * Best-effort coercion of an untyped `polling_metadata.live_release_evidence`
 * value (typed `unknown` on ComputeJob) into a {@link LiveReleaseEvidence}.
 * Returns null when the value is not a usable object.
 */
export function asLiveReleaseEvidence(value: unknown): LiveReleaseEvidence | null {
  if (!value || typeof value !== 'object') return null;
  return value as LiveReleaseEvidence;
}
