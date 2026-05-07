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
  type EdgeRaw = Record<string, unknown>;
  const edgesConsidered = Array.isArray(selfReviewOutcome.edges_considered)
    ? (selfReviewOutcome.edges_considered as unknown[]).filter(
        (e): e is EdgeRaw =>
          typeof e === 'object' && e !== null && typeof (e as EdgeRaw).case === 'string',
      )
    : [];
  const incomplete = categories.includes('incomplete_review');
  if (!incomplete && truncatedFilesRead.length === 0 && reviewerFindings.length === 0 && blindSpots.length === 0 && edgesConsidered.length === 0) return '';

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
  if (edgesConsidered.length > 0) {
    const unhandled = edgesConsidered.filter(e => e.handled === false);
    const handled = edgesConsidered.filter(e => e.handled !== false);
    const originSuffix = (e: EdgeRaw): string => {
      const origin = e.origin;
      return origin === 'author' || origin === 'reviewer' ? ` · ${origin}` : '';
    };
    const edgeLines: string[] = ['- Edge cases enumerated:'];
    for (const e of unhandled) {
      const note = typeof e.blind_spot === 'string' ? ` — ${e.blind_spot}` : ' — unhandled, no explanation given';
      edgeLines.push(`  - [UNHANDLED${originSuffix(e)}] ${String(e.case)}${note}`);
    }
    for (const e of handled) {
      const evidence = typeof e.evidence === 'string' ? ` (${e.evidence})` : '';
      edgeLines.push(`  - [handled${originSuffix(e)}] ${String(e.case)}${evidence}`);
    }
    lines.push(edgeLines.join('\n'));
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

// Format project-wide blind spot theme aggregation (from the backend /api/admin/sr-blind-spots
// endpoint) into a prompt section so reviewers see which areas the agent has been most
// uncertain about across recent tasks. Returns '' when themes is empty.
export function formatProjectBlindSpotSummary(
  themes: Array<{ theme: string; count: number }>,
): string {
  if (themes.length === 0) return '';
  const top = themes.slice(0, 8);
  const rows = top.map(t => `- ${t.theme} (${t.count} task${t.count === 1 ? '' : 's'})`).join('\n');
  return (
    `**Project-wide self-review blind spots (last 14 days — areas where self-review has been most uncertain):**\n` +
    `${rows}\n` +
    `Focus extra scrutiny on files or patterns that match these themes.\n\n`
  );
}

// Generate the execution-evidence category instructions for injection into the agent's
// self-review prompt. Instructs the agent to populate three sub-categories in
// check_results — tests_executed_cleanly, lint_executed_cleanly, typecheck_executed_cleanly —
// and to PASS only when the command was actually run on this VM with a zero exit code.
export function formatExecutionEvidenceForSrPrompt(): string {
  return (
    `## Execution-evidence categories (required in check_results)\n` +
    `You MUST populate these three sub-categories every time you write a self-review:\n\n` +
    `- **tests_executed_cleanly** — did the test suite run on this VM and exit 0?\n` +
    `- **lint_executed_cleanly** — did the linter / style checker run on this VM and exit 0?\n` +
    `- **typecheck_executed_cleanly** — did the type-checker run on this VM and exit 0?\n\n` +
    `Each entry MUST have this shape:\n` +
    `  { "passed": <bool>, "severity": "major", "evidence": "exit_code=0 from <command>" }\n\n` +
    `**PASS rules (both must hold):**\n` +
    `1. You personally ran the command on this VM during this task — not in a prior session, not assumed from context.\n` +
    `2. You captured the output and the exit code was 0.\n\n` +
    `**FAIL rules:**\n` +
    `- If you did NOT run the command, set passed=false, severity="major", evidence="command not run".\n` +
    `- If the command exited non-zero, set passed=false, severity="major", evidence="exit_code=<N> from <command>".\n` +
    `- Claiming a command passed when you have no execution log is a FAIL with severity=major.\n\n` +
    `Do not skip these categories or mark them passed without a real exit-code record.\n\n`
  );
}

// Format blind-spot themes for injection into the agent's own self-review prompt.
// Unlike formatProjectBlindSpotSummary (which addresses a reviewer reading the diff),
// this section is written TO the agent — framing themes as categories it has historically
// missed to make the agent more skeptical of its own PASS verdicts.
// Returns '' when themes is empty, null, or undefined.
export function formatSrBlindSpotThemesForSrPrompt(
  themes: Array<{ theme: string; count: number }> | null | undefined,
): string {
  if (!themes || themes.length === 0) return '';
  const top = themes.slice(0, 8);
  const rows = top.map(t => `- ${t.theme} (${t.count} task${t.count === 1 ? '' : 's'})`).join('\n');
  return (
    `## Your historical blind spots\n` +
    `These are areas where your self-review has historically missed issues that a reviewer later caught. ` +
    `Apply extra skepticism to PASS verdicts in these categories — if evidence is thin, lean toward flagging rather than clearing:\n` +
    `${rows}\n\n`
  );
}
