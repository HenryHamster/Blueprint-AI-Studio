// Format a blind-spots section describing what the create task's self-review
// verified (via truncated_files_read) versus which changed files it did not
// re-read. When self-review was flagged incomplete_review (diff truncated and
// the retry still reported zero files read), the reviewer needs to scrutinize
// unverified files — the self-review's PASS verdict did not examine them.
// Also surfaces reviewer_findings written by the fresh-reviewer pass when
// project_metadata.enable_fresh_reviewer is enabled (see standard.ts).
// Returns '' when no self-review outcome is present or nothing is worth
// surfacing.
export function formatSelfReviewBlindSpots(
  selfReviewOutcome: Record<string, unknown> | undefined,
  changedFilePaths: string[],
): string {
  if (!selfReviewOutcome) return '';
  const categories = Array.isArray(selfReviewOutcome.categories)
    ? (selfReviewOutcome.categories as unknown[]).filter((c): c is string => typeof c === 'string')
    : [];
  const truncatedFilesRead = Array.isArray(selfReviewOutcome.truncated_files_read)
    ? (selfReviewOutcome.truncated_files_read as unknown[]).filter((f): f is string => typeof f === 'string')
    : [];
  type FindingRaw = Record<string, unknown>;
  const reviewerFindings = Array.isArray(selfReviewOutcome.reviewer_findings)
    ? (selfReviewOutcome.reviewer_findings as unknown[]).filter(
        (f): f is FindingRaw =>
          typeof f === 'object' && f !== null &&
          typeof (f as FindingRaw).category === 'string' &&
          typeof (f as FindingRaw).description === 'string',
      )
    : [];
  const blindSpots = Array.isArray(selfReviewOutcome.blind_spots)
    ? (selfReviewOutcome.blind_spots as unknown[]).filter((s): s is string => typeof s === 'string' && s.length > 0)
    : [];
  const incomplete = categories.includes('incomplete_review');
  if (!incomplete && truncatedFilesRead.length === 0 && reviewerFindings.length === 0 && blindSpots.length === 0) return '';

  const verifiedSet = new Set(truncatedFilesRead);
  const unverified = changedFilePaths.filter(p => p && !verifiedSet.has(p));
  const verdict = typeof selfReviewOutcome.verdict === 'string' ? selfReviewOutcome.verdict : undefined;

  const lines: string[] = ['**Self-review coverage (from the create task):**'];
  if (verdict) lines.push(`- Self-review verdict: ${verdict}`);
  if (incomplete) {
    lines.push('- Self-review was marked INCOMPLETE — the diff was truncated and the agent did not re-read all changed files even after retry. The PASS verdict does not cover unverified files; treat them as blind spots.');
  }
  if (truncatedFilesRead.length > 0) {
    lines.push(`- Files the self-review verified (already checked — spend less effort here):\n${truncatedFilesRead.map(f => `  - ${f}`).join('\n')}`);
  }
  if (unverified.length > 0) {
    lines.push(`- Files NOT verified by self-review (prioritize careful review of these):\n${unverified.map(f => `  - ${f}`).join('\n')}`);
  }
  if (reviewerFindings.length > 0) {
    const sevOrder = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
    const sorted = [...reviewerFindings].sort((a, b) => {
      const ai = sevOrder.indexOf(String(a.severity));
      const bi = sevOrder.indexOf(String(b.severity));
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
    lines.push(
      `- Fresh-reviewer findings (a second read-only session flagged these before ./done — verify each was addressed or is a false positive):\n` +
      sorted.map(f => `  - [${String(f.severity)}] ${String(f.category)}: ${String(f.description)}`).join('\n'),
    );
  }
  let blindSpotsBlock = '';
  if (blindSpots.length > 0) {
    const capped = blindSpots.slice(0, 5).map(s => (s.length > 200 ? s.slice(0, 200) + '…' : s));
    blindSpotsBlock =
      `\n\n**Author-flagged uncertainty (verify each):**\n` +
      capped.map(s => `- ${s}`).join('\n');
  }
  return lines.join('\n') + blindSpotsBlock + '\n\n';
}
