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
// self-review prompt. Instructs the agent to populate four sub-categories in
// check_results — tests_executed_cleanly, lint_executed_cleanly, typecheck_executed_cleanly,
// and test_assertion_quality — and to PASS only when the command was actually run on
// this VM with a zero exit code (or assertions were inspected for the quality check).
export function formatExecutionEvidenceForSrPrompt(): string {
  return (
    `## Execution-evidence categories (required in check_results)\n` +
    `You MUST populate these four sub-categories every time you write a self-review:\n\n` +
    `- **tests_executed_cleanly** — did the test suite run on this VM and exit 0?\n` +
    `- **lint_executed_cleanly** — did the linter / style checker run on this VM and exit 0?\n` +
    `- **typecheck_executed_cleanly** — did the type-checker run on this VM and exit 0?\n` +
    `- **test_assertion_quality** — for each test added or modified in this PR: does the assertion exercise the specific behavior changed (e.g., a particular output value, a side-effect, a specific error type/message), or is it essentially smoke (e.g., "returns a value", "no exception", asserts truthiness only)? If a test mocks the unit under test, is the mock surface narrow enough that the test still validates the changed logic, or is the changed code path effectively unverified?\n\n` +
    `The first three entries MUST have this shape:\n` +
    `  { "passed": <bool>, "severity": "major", "evidence": "exit_code=0 from <command>" }\n\n` +
    `**test_assertion_quality** uses: { "passed": <bool>, "severity": "minor"|"major", "evidence": "<test_file>:<line> asserts <specific value/behavior>, not just truthiness" }\n` +
    `Set severity="major" when ALL new/modified tests are smoke-only or mock the unit under test entirely; "minor" when at least one meaningful assertion exists but some tests are weak.\n\n` +
    `**PASS rules for tests_executed_cleanly / lint_executed_cleanly / typecheck_executed_cleanly (both must hold):**\n` +
    `1. You personally ran the command on this VM during this task — not in a prior session, not assumed from context.\n` +
    `2. You captured the output and the exit code was 0.\n\n` +
    `**FAIL rules (command categories):**\n` +
    `- If you did NOT run the command, set passed=false, severity="major", evidence="command not run".\n` +
    `- If the command exited non-zero, set passed=false, severity="major", evidence="exit_code=<N> from <command>".\n` +
    `- Claiming a command passed when you have no execution log is a FAIL with severity=major.\n\n` +
    `Do not skip these categories or mark them passed without a real exit-code record.\n\n`
  );
}

// Format concrete recent miss examples for injection into the agent's self-review prompt.
// Consumes the backend-emitted shape (see backend/app/routers/agent.py
// `response["project_context"]["sr_recent_miss_examples"]`):
//   { [category]: [{ severity, desc }, ...], ... }
// The backend already ranks categories by weighted_miss_rate and caps to top 3
// categories / 3 examples each / ~1200 chars, so this formatter trusts that ordering
// and only applies defensive safety caps. Returns '' when no examples are available.
export function formatSrRecentMissExamplesForSrPrompt(
  miss: Record<string, Array<{ desc: string; severity: string }>> | null | undefined,
): string {
  if (!miss || typeof miss !== 'object') return '';

  const MAX_CATS = 3;
  const MAX_EXAMPLES_PER_CAT = 3;
  const MAX_DESC_CHARS = 200;
  const MAX_TOTAL_CHARS = 1200;

  const catsWithExamples = Object.entries(miss)
    .filter(([, examples]) => Array.isArray(examples) && examples.length > 0)
    .slice(0, MAX_CATS);

  if (catsWithExamples.length === 0) return '';

  const sections: string[] = [];
  let totalChars = 0;

  for (const [cat, examples] of catsWithExamples) {
    if (totalChars >= MAX_TOTAL_CHARS) break;
    const rows = examples
      .slice(0, MAX_EXAMPLES_PER_CAT)
      .map(ex => {
        const desc = ex.desc.length > MAX_DESC_CHARS ? ex.desc.slice(0, MAX_DESC_CHARS) + '…' : ex.desc;
        return `  - [${ex.severity}] ${desc}`;
      })
      .join('\n');
    const section = `**${cat}:**\n${rows}`;
    if (totalChars + section.length > MAX_TOTAL_CHARS) break;
    sections.push(section);
    totalChars += section.length;
  }

  if (sections.length === 0) return '';

  return (
    `## Concrete misses from the last cycle\n` +
    `These are real reviewer findings the self-review missed for this project's worst-performing categories. ` +
    `Apply heightened scrutiny when you see similar code patterns:\n\n` +
    sections.join('\n\n') +
    `\n\n`
  );
}

// Format a persistent blind-spot tightening hint for injection into the agent's
// self-review prompt. Consumed when a category's reviewer-disagreement rate
// exceeds the threshold for ≥2 consecutive aggregation windows (computed by
// sr_blind_spots.compute_persistent_sr_blind_spot_hint in the backend).
// Returns '' when no persistent categories are present or hint is absent.
export function formatPersistentBlindSpotHintForSrPrompt(
  data: {
    categories: Array<{ category: string; task_type: string; rate: number }>;
    hint: string | null;
  } | null | undefined,
): string {
  if (!data || !Array.isArray(data.categories) || data.categories.length === 0) return '';
  if (!data.hint) return '';
  return (
    `## Persistent Self-Review Blind Spots\n` +
    `${data.hint}\n\n`
  );
}

// Format a pre-execution loosen-hint for categories with a reliably high false-positive
// rate. When fp_rate >= 0.5 and sample_size >= 10, the agent has chronically over-flagged
// that category — instruct it to require concrete evidence before flagging there.
// This is the symmetric counterpart to the tightening hints: tighten where we miss,
// loosen where we over-flag. Returns '' when no category meets both thresholds.
export function formatHighFpRateLoosenHintForSrPrompt(
  categories: Record<string, { false_positive_rate?: number; precision?: number; sr_flag_sample_size?: number }> | null | undefined,
): string {
  if (!categories || typeof categories !== 'object') return '';
  const rows = Object.entries(categories)
    .filter(([, d]) => (d.false_positive_rate ?? 0) >= 0.5 && (d.sr_flag_sample_size ?? 0) >= 10)
    .map(([cat, d]) => `- ${cat}: ${Math.round((d.false_positive_rate ?? 0) * 100)}% false-positive rate (n=${d.sr_flag_sample_size ?? 0})`);
  if (rows.length === 0) return '';
  return (
    `## Over-flagged categories — require concrete evidence before flagging\n` +
    `These categories have a sustained high false-positive rate in this project (≥50%, n≥10). ` +
    `Do NOT flag unless you have concrete, specific evidence — vague concerns add noise that trains operators to discount SR output:\n` +
    rows.join('\n') + `\n\n`
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

// Format co-failure pairs for injection into the agent's self-review prompt.
// When categories A and B are missed together at a significantly higher rate than
// by chance (lift > 1), fixing A requires re-checking B — single prompt fix addressable.
// Returns '' when pairs is empty, null, or undefined, or below a lift threshold.
// Shape: [{ cat_a, cat_b, co_fail_count, p_b_given_a, base_rate_b, lift }, ...]
export function formatSrCoFailurePairsForSrPrompt(
  pairs: Array<{ cat_a: string; cat_b: string; lift: number }> | null | undefined,
): string {
  if (!pairs || pairs.length === 0) return '';
  const MIN_LIFT_THRESHOLD = 1.5;
  const filtered = pairs.filter(p => p.lift >= MIN_LIFT_THRESHOLD);
  if (filtered.length === 0) return '';
  const rows = filtered.slice(0, 5).map(p => `- When you flag/miss **${p.cat_a}**, re-check **${p.cat_b}** before marking PASS (lift ${p.lift.toFixed(1)}x)`).join('\n');
  return (
    `## Co-failure pairs — re-check before marking PASS\n` +
    `These categories frequently miss together. After you finish one, revisit its partner:\n` +
    `${rows}\n\n`
  );
}

// Format severity calibration hints for injection into the agent's self-review prompt.
// Shows categories where the agent has been chronically under-rating (should lean toward major)
// or over-rating (should lean toward minor) severity. When a category's under_rated/total
// or over_rated/total ratio >= 0.30 and total >= 5, surfaces the pattern with a brief nudge.
// Caps to top 3 categories by ratio. Returns '' when no categories meet thresholds.
export function formatSeverityCalibrationHintForSrPrompt(
  categories: Record<string, { severity_calibration?: { under_rated?: number; over_rated?: number; matched?: number; total?: number } }> | null | undefined,
): string {
  if (!categories || typeof categories !== 'object') return '';

  const RATIO_THRESHOLD = 0.30;
  const MIN_SAMPLES = 5;
  const MAX_CATS = 3;

  const underRated: Array<{ cat: string; ratio: number; total: number }> = [];
  const overRated: Array<{ cat: string; ratio: number; total: number }> = [];

  for (const [cat, data] of Object.entries(categories)) {
    const cal = data.severity_calibration;
    if (!cal || typeof cal !== 'object') continue;

    const total = typeof cal.total === 'number' ? cal.total : 0;
    if (total < MIN_SAMPLES) continue;

    const underCount = typeof cal.under_rated === 'number' ? cal.under_rated : 0;
    const overCount = typeof cal.over_rated === 'number' ? cal.over_rated : 0;

    const underRatio = underCount / total;
    const overRatio = overCount / total;

    if (underRatio >= RATIO_THRESHOLD) {
      underRated.push({ cat, ratio: underRatio, total });
    }
    if (overRatio >= RATIO_THRESHOLD) {
      overRated.push({ cat, ratio: overRatio, total });
    }
  }

  // Sort by ratio descending and cap to top 3
  underRated.sort((a, b) => b.ratio - a.ratio);
  overRated.sort((a, b) => b.ratio - a.ratio);

  const topUnderRated = underRated.slice(0, MAX_CATS);
  const topOverRated = overRated.slice(0, MAX_CATS);

  if (topUnderRated.length === 0 && topOverRated.length === 0) return '';

  const lines: string[] = ['## Severity calibration tightening'];
  if (topUnderRated.length > 0) {
    lines.push(`You have under-rated severity in these categories (leaning toward MINOR when the issue warrants MAJOR). Lean toward MAJOR when in doubt:`);
    for (const item of topUnderRated) {
      const percent = Math.round(item.ratio * 100);
      lines.push(`- ${item.cat}: ${percent}% under-rated (n=${item.total})`);
    }
  }
  if (topOverRated.length > 0) {
    if (topUnderRated.length > 0) lines.push('');
    lines.push(`You have over-rated severity in these categories (leaning toward MAJOR when MINOR is more appropriate). Lean toward MINOR when in doubt:`);
    for (const item of topOverRated) {
      const percent = Math.round(item.ratio * 100);
      lines.push(`- ${item.cat}: ${percent}% over-rated (n=${item.total})`);
    }
  }

  return lines.join('\n') + '\n\n';
}
