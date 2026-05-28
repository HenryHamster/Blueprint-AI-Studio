import fs from 'fs';
import type WebSocket from 'ws';
import { sendLog, runClaudeCode, git, apiPost } from '../index.js';

// ── Fresh-reviewer constants ──────────────────────────────────────────────────

const FRESH_REVIEWER_MIN_LINES = 20;
const FRESH_REVIEWER_MEDIUM_EFFORT_THRESHOLD = 300;
const SENSITIVE_PATHS = [
  'backend/app/auth',
  'backend/app/security',
  'backend/app/routers/agent',
  'backend/app/jwt_revocation',
];

export const VIEWPORT_INFRA_SKIP_GUIDANCE = `## Viewport gate: infra-skip sentinel

If \`./check\` output contains \`VIEWPORT_GATE_INFRA_UNAVAILABLE:\` OR the
"admin-ui playwright chromium" bootstrap step fails AND \`docker info\` also
errors (Docker unavailable), this is an infrastructure shortfall — NOT a code
defect. Chromium and its system-library dependencies cannot be installed by a
code edit on this VM. Call \`./blocked "VIEWPORT_GATE_INFRA_UNAVAILABLE: <reason>"\`
immediately. Do NOT attempt to install Chromium, modify viewport tests to bypass
the browser requirement, or weaken test assertions to work around the missing runtime.
`;

export const SR_SKIP_REASONS = {
  DIFF_TOO_SMALL: 'diff_too_small',
  REVIEWER_SESSION_FAILED: 'reviewer_session_failed',
  REVIEWER_NO_FINDINGS_PARSE: 'reviewer_no_findings_parse',
  JSON_PARSE_FAILED: 'json_parse_failed',
  JSON_MISSING: 'json_missing',
  NO_BASELINE: 'no_baseline',
  AUTO_REMEDIATION: 'auto_remediation',
} as const;

function writeSrSkipReason(taskId: string, reason: string): void {
  try { fs.writeFileSync(`/tmp/self_review_skip_reason_${taskId}.txt`, reason, { mode: 0o644 }); } catch { /* best-effort */ }
}

export type SRCatchRateExample = { severity: string; desc: string };
export type SRCatchRateCategory = {
  miss_rate?: number;
  weighted_miss_rate?: number;
  false_positive_rate?: number;
  precision?: number;
  sr_flag_sample_size?: number;
  examples?: SRCatchRateExample[];
  time_to_miss_detect_lone_checker?: boolean;
  engagement_thinning?: boolean;
};

/**
 * Determines the fresh-reviewer effort level based on risk signals.
 * Returns 'medium' when (a) loneCheckerRecovery=true, (b) incompleteSrRecovery=true,
 * (c) diff exceeds threshold (>300 changed lines), or (d) any sensitive file is touched.
 * Otherwise returns 'low'.
 */
export function pickFreshReviewerEffort(opts: {
  loneCheckerRecovery?: boolean;
  incompleteSrRecovery?: boolean;
  changedLineCount?: number;
  changedFiles?: string[];
}): 'low' | 'medium' {
  const { loneCheckerRecovery, incompleteSrRecovery, changedLineCount = 0, changedFiles = [] } = opts;

  // Escalate on recovery paths
  if (loneCheckerRecovery || incompleteSrRecovery) {
    return 'medium';
  }

  // Escalate on large diffs
  if (changedLineCount > FRESH_REVIEWER_MEDIUM_EFFORT_THRESHOLD) {
    return 'medium';
  }

  // Escalate if any sensitive file is touched
  for (const file of changedFiles) {
    for (const sensitivePrefix of SENSITIVE_PATHS) {
      if (file.startsWith(sensitivePrefix)) {
        return 'medium';
      }
    }
  }

  return 'low';
}

function formatFreshReviewerCalibrationSection(
  projectContext: Record<string, unknown> | undefined,
): string {
  const srCatchRate = projectContext?.self_review_catch_rate as {
    categories?: Record<string, SRCatchRateCategory>;
  } | null | undefined;
  const categories = srCatchRate?.categories;
  if (!categories || Object.keys(categories).length === 0) return '';

  const ranked = Object.entries(categories)
    .filter(([, data]) => typeof data.miss_rate === 'number' && data.miss_rate > 0)
    .sort((a, b) => (b[1].weighted_miss_rate ?? b[1].miss_rate ?? 0) - (a[1].weighted_miss_rate ?? a[1].miss_rate ?? 0))
    .slice(0, 3);

  if (ranked.length === 0) return '';

  const lines: string[] = [
    'Calibration from prior self-review misses:',
  ];
  for (const [category, data] of ranked) {
    const missRate = Math.round((data.miss_rate ?? 0) * 100);
    const weightedMissRate = data.weighted_miss_rate;
    const rateText = weightedMissRate !== undefined
      ? `${missRate}% missed (${Math.round(weightedMissRate * 100)}% severity-weighted)`
      : `${missRate}% missed`;
    lines.push(`- ${category}: ${rateText}`);
    if ((data.false_positive_rate ?? 0) >= 0.5 && (data.sr_flag_sample_size ?? 0) >= 10) {
      const falsePositiveRate = Math.round((data.false_positive_rate ?? 0) * 100);
      const precision = data.precision !== undefined ? `${Math.round(data.precision * 100)}% precision; ` : '';
      lines.push(`  - ${falsePositiveRate}% of past flags were not confirmed by review (${precision}sample size ${data.sr_flag_sample_size ?? 0}) — apply lighter scrutiny unless evidence is concrete.`);
    }
    const examples = (data.examples || []).slice(0, 2);
    for (const ex of examples) {
      lines.push(`  - [${ex.severity}] ${ex.desc}`);
    }
  }
  lines.push('Scrutinize these categories extra carefully.');
  return `${lines.join('\n')}\n\n`;
}

export function getRegressingCategories(
  projectContext: Record<string, unknown> | undefined,
): string[] {
  const srCatchRate = projectContext?.self_review_catch_rate as {
    categories?: Record<string, SRCatchRateCategory>;
  } | null | undefined;
  const categories = srCatchRate?.categories;
  if (!categories) return [];
  return Object.entries(categories)
    .filter(([, data]) => typeof data.miss_rate === 'number' && data.miss_rate > 0)
    .sort((a, b) => (b[1].weighted_miss_rate ?? b[1].miss_rate ?? 0) - (a[1].weighted_miss_rate ?? a[1].miss_rate ?? 0))
    .map(([cat]) => cat);
}

export function getThinningCategories(
  projectContext: Record<string, unknown> | undefined,
): string[] {
  const srCatchRate = projectContext?.self_review_catch_rate as {
    categories?: Record<string, SRCatchRateCategory>;
  } | null | undefined;
  const categories = srCatchRate?.categories;
  if (!categories) return [];
  return Object.entries(categories)
    .filter(([, data]) => data.engagement_thinning === true)
    .map(([cat]) => cat);
}

/**
 * Returns lone-checker categories from project_context that overlap with the
 * task's expected risk surface (derived from related_files path prefixes).
 * Used to auto-trigger a fresh-reviewer pass when SR is the sole corroborator
 * for a high-risk category.
 */
export function loneCheckerCategoriesForTask(
  projectContext: Record<string, unknown> | undefined,
  relatedFiles: string[],
): string[] {
  const srCatchRate = projectContext?.self_review_catch_rate as {
    categories?: Record<string, SRCatchRateCategory>;
  } | null | undefined;
  const categories = srCatchRate?.categories;
  if (!categories) return [];
  const loneCheckers = Object.entries(categories)
    .filter(([, data]) => data.time_to_miss_detect_lone_checker === true)
    .map(([cat]) => cat);
  if (loneCheckers.length === 0) return [];
  const riskSurface = relatedFiles.some(f => f.startsWith('backend/'))
    ? new Set(['security', 'regression_risk', 'correctness', 'error-handling'])
    : relatedFiles.some(f => f.startsWith('admin-ui/'))
    ? new Set(['security', 'correctness', 'type-safety', 'regression_risk'])
    : relatedFiles.some(f => f.startsWith('agent/'))
    ? new Set(['security', 'regression_risk', 'correctness', 'error-handling', 'type-safety'])
    : null;
  return riskSurface ? loneCheckers.filter(cat => riskSurface.has(cat)) : loneCheckers;
}

/**
 * Augments check_results entries with fp_rate_hint when a category has reliable
 * false-positive rate data (fp_rate >= 50% and sample_size >= 10). The hint is used
 * by sr-pr-section.js to annotate findings when rendered in the PR body.
 */
function augmentCheckResultsWithFpRateHints(
  checkResults: Record<string, Record<string, unknown>>,
  projectContext: Record<string, unknown> | undefined,
): void {
  const srCatchRate = projectContext?.self_review_catch_rate as {
    categories?: Record<string, SRCatchRateCategory>;
  } | null | undefined;
  const categories = srCatchRate?.categories;
  if (!categories) return;

  for (const [category, entry] of Object.entries(checkResults)) {
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
    const categoryData = categories[category];
    // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
    if (!categoryData) continue;
    const fpRate = categoryData.false_positive_rate ?? 0;
    const sampleSize = categoryData.sr_flag_sample_size ?? 0;
    if (fpRate >= 0.5 && sampleSize >= 10) {
      const fpPercent = Math.round(fpRate * 100);
      entry.fp_rate_hint = `(FP rate ${fpPercent}% in this project, n=${sampleSize})`;
    }
  }
}

/**
 * Runs a second read-only Claude session to review the current diff as an
 * adversarial reviewer. Findings are merged into /tmp/self_review_<taskId>.json:
 * reviewer-flagged categories override passing self-attestations, and findings
 * appear in a new `reviewer_findings` array. Skips automatically for tiny diffs
 * or when the diff file is unavailable.
 */
export async function runFreshReviewerPass(
  taskId: string,
  taskTitle: string,
  taskDescription: string,
  relatedFiles: string[],
  ws: WebSocket | null,
  statusRef: { value: string },
  model?: string,
  projectContext?: Record<string, unknown>,
  incompleteSrRecovery?: boolean,
  loneCheckerRecovery?: boolean,
  operatorRequestedRecovery?: boolean,
  originalTaskId?: string,
): Promise<void> {
  let baseCommit = '';
  try { baseCommit = fs.readFileSync(`/tmp/base_commit_${taskId}`, 'utf-8').trim(); } catch { /* ok */ }
  let diff = '';
  try {
    diff = git(['diff', ...(baseCommit ? [`${baseCommit}..HEAD`] : ['HEAD~1..HEAD']), '--unified=3']);
  } catch { /* no prior commit */ }
  if (!diff) return;

  const changed = diff.split('\n').filter(l => /^[+-]/.test(l) && !/^[+-]{3}/.test(l)).length;
  if (changed < FRESH_REVIEWER_MIN_LINES) {
    sendLog(ws, `[FreshReview] Diff too small (${changed} lines) — skipping reviewer pass.\n`);
    writeSrSkipReason(taskId, SR_SKIP_REASONS.DIFF_TOO_SMALL);
    return;
  }

  sendLog(ws, `[FreshReview] Running fresh-reviewer pass (${changed} changed lines)...\n`);
  const prevStatus = statusRef.value;
  statusRef.value = 'Running fresh-reviewer pass...';

  const MAX_DIFF = 20_000;
  const diffSnippet = diff.length > MAX_DIFF ? diff.slice(0, MAX_DIFF) + '\n...[truncated]' : diff;
  const calibrationSection = formatFreshReviewerCalibrationSection(projectContext);
  const prompt = `You are reviewing a PR for a colleague. The author thinks it is done. Find what they missed.

Task: ${taskTitle}
Description: ${taskDescription.slice(0, 800)}
Declared scope: ${relatedFiles.join(', ') || 'unspecified'}

${calibrationSection}Diff:
${diffSnippet}

Check specifically for:
- regression_risk: removed exports, changed defaults, removed null guards, broken contracts
- scope-drift: any modified file outside the Declared scope set above is off-scope, UNLESS it is an incidental allowlisted file — lockfiles (package-lock.json, poetry.lock, Cargo.lock, go.sum, yarn.lock, pnpm-lock.yaml), build-generated outputs, .gitignore, or a sibling test file mirroring a declared source file (e.g. foo.test.ts next to foo.ts, test_foo.py next to foo.py). Flag every off-scope file by path under scope-drift.
- correctness: logic bugs, off-by-one errors, race conditions, edge cases
- error-handling: unhandled exceptions, missing null checks, async error paths
- security: injection attacks, auth flaws, exposed secrets, unvalidated user inputs
- type-safety: unsafe casts, any-typed externals, missing null guards in TypeScript
- architecture: boundary violations, coupling regressions, abstraction leaks
- test-coverage: new behavior without tests, untested edge cases or error paths
- test_assertion_quality: for each test added or modified, does the assertion verify the specific behavior changed (a particular output value, side-effect, error type/message), or is it smoke-only ("returns something", "no exception", truthiness)? Flag tests that mock the unit under test so thoroughly that the changed logic is effectively unverified.

Also enumerate edge cases YOU spot from a fresh read of the diff. Use the SAME shape the
implementing author uses for their \`edges_considered\` field — \`{case, handled, evidence?, blind_spot?}\`.
List cases the author may have missed entirely, cases the author marked handled where you
disagree (set \`handled: false\` and explain in \`blind_spot\`), and any additional unhandled
cases. Cite file + line in \`evidence\` when claiming an edge is handled.

Output ONLY a JSON object:
{"reviewer_findings": [{"category": "<id>", "severity": "CRITICAL|HIGH|MEDIUM|LOW", "description": "..."}],
 "reviewer_edges_considered": [{"case": "...", "handled": true|false, "evidence": "...", "blind_spot": "..."}]}
Category IDs: correctness, error-handling, security, type-safety, scope-drift, regression_risk, architecture, test-coverage, test_assertion_quality
If no issues found: {"reviewer_findings": [], "reviewer_edges_considered": []}`;

  const changedFiles = extractChangedFilesFromDiff(diff);
  const effort = pickFreshReviewerEffort({
    loneCheckerRecovery,
    incompleteSrRecovery,
    changedLineCount: changed,
    changedFiles: Array.from(changedFiles),
  });

  const { exitCode, lastResultText } = await runClaudeCode(
    prompt, ws, statusRef, model, effort,
    ['Write', 'Edit', 'NotebookEdit'],
  );
  statusRef.value = prevStatus;

  if (exitCode !== 0) {
    sendLog(ws, '[FreshReview] Reviewer session failed — skipping merge.\n');
    writeSrSkipReason(taskId, SR_SKIP_REASONS.REVIEWER_SESSION_FAILED);
    return;
  }

  const parsed = extractSelfReviewJson(lastResultText);
  if (!parsed || !Array.isArray(parsed.reviewer_findings)) {
    sendLog(ws, '[FreshReview] No parseable findings — skipping merge.\n');
    writeSrSkipReason(taskId, SR_SKIP_REASONS.REVIEWER_NO_FINDINGS_PARSE);
    return;
  }

  const findings = parsed.reviewer_findings as Array<{ category: string; severity: string; description: string }>;
  const groundedFindings: typeof findings = [];
  let hallucinatedCount = 0;
  const phantomPaths: string[] = [];

  for (const f of findings) {
    if (isHallucinatedFinding(f, changedFiles)) {
      hallucinatedCount++;
      if (phantomPaths.length < 5) {
        for (const p of extractCandidateFilePaths(f.description)) {
          if (!changedFiles.has(p) && phantomPaths.length < 5) {
            phantomPaths.push(p);
          }
        }
      }
    } else {
      groundedFindings.push(f);
    }
  }

  if (hallucinatedCount > 0) {
    sendLog(ws, `[FreshReview] ${groundedFindings.length} grounded finding(s), ${hallucinatedCount} filtered (phantom file refs).\n`);
  } else {
    sendLog(ws, `[FreshReview] ${findings.length} reviewer finding(s).\n`);
  }

  const srPath = `/tmp/self_review_${taskId}.json`;
  let sr: Record<string, unknown> = {};
  try { sr = JSON.parse(fs.readFileSync(srPath, 'utf-8')) as Record<string, unknown>; } catch { /* may not exist */ }

  const checkResults = (sr.check_results as Record<string, Record<string, unknown>> | undefined) ?? {};
  const checkResultsLookup = checkResults as Record<string, Record<string, unknown> | undefined>;

  // Backfill origin='sr' on existing agent self-review entries that lack an origin field.
  for (const [, entry] of Object.entries(checkResultsLookup)) {
    if (entry && !('origin' in entry)) {
      entry.origin = 'sr';
    }
  }

  // Merge fresh-reviewer findings, tagging each with origin='reviewer'.
  for (const f of groundedFindings) {
    const existing = checkResultsLookup[f.category];
    if (!existing || existing.passed === true) {
      const entry: Record<string, unknown> = { passed: false, issue: `[reviewer] ${f.description}` };
      if (incompleteSrRecovery) {
        entry.origin = 'reviewer_recovery_after_incomplete_sr';
      } else if (loneCheckerRecovery) {
        entry.origin = 'lone_checker_recovery';
      } else if (operatorRequestedRecovery) {
        entry.origin = 'operator_requested_recovery';
      } else if (existing?.passed === true) {
        entry.origin = 'reviewer_disagreement';
      } else {
        entry.origin = 'reviewer';
      }
      checkResults[f.category] = entry;
    }
  }
  sr.reviewer_findings = findings;
  sr.check_results = checkResults;
  if (incompleteSrRecovery) {
    sr.reviewer_recovery_origin = 'reviewer_recovery_after_incomplete_sr';
  } else if (loneCheckerRecovery) {
    sr.reviewer_recovery_origin = 'lone_checker_recovery';
  } else if (operatorRequestedRecovery) {
    sr.reviewer_recovery_origin = 'operator_requested_recovery';
  }

  // Merge reviewer-enumerated edges. Filter defensively to entries with a
  // string `case` field, then tag with origin: 'reviewer'. Author edges
  // (if any) are backfilled with origin: 'author' and the combined list is
  // written to `edges_considered`. The reviewer-only list is also exposed
  // top-level as `reviewer_edges_considered` for consumers that want to
  // inspect just the reviewer pass.
  const rawReviewerEdges = parsed.reviewer_edges_considered;
  if (Array.isArray(rawReviewerEdges)) {
    type EdgeRaw = Record<string, unknown>;
    const reviewerEdges = (rawReviewerEdges as unknown[])
      .filter((e): e is EdgeRaw => typeof e === 'object' && e !== null && typeof (e as EdgeRaw).case === 'string')
      .map(e => {
        const out: Record<string, unknown> = {
          case: e.case,
          handled: typeof e.handled === 'boolean' ? e.handled : false,
          origin: 'reviewer',
        };
        if (typeof e.evidence === 'string') out.evidence = e.evidence;
        if (typeof e.blind_spot === 'string') out.blind_spot = e.blind_spot;
        return out;
      });

    const existingEdges = Array.isArray(sr.edges_considered)
      ? (sr.edges_considered as unknown[]).filter(
          (e): e is Record<string, unknown> =>
            typeof e === 'object' && e !== null && typeof (e as Record<string, unknown>).case === 'string',
        )
      : [];
    const taggedAuthorEdges = existingEdges.map(e =>
      typeof e.origin === 'string' ? e : { ...e, origin: 'author' },
    );
    sr.edges_considered = [...taggedAuthorEdges, ...reviewerEdges];
    sr.reviewer_edges_considered = reviewerEdges;
  }

  // Track engagement gaps: regressing categories that passed SR without a non-trivial
  // reasoning string. Stored top-level as engagement_gaps for downstream metric tracking.
  const regressingCats = getRegressingCategories(projectContext);
  if (regressingCats.length > 0) {
    const engagementGaps: string[] = [];
    const srCheckResults = sr.check_results as Record<string, Record<string, unknown>> | undefined;
    for (const cat of regressingCats) {
      const entry = srCheckResults?.[cat];
      if (entry && entry.passed === true) {
        const reasoning = typeof entry.reasoning === 'string' ? entry.reasoning.trim() : '';
        if (reasoning.length < 10) engagementGaps.push(cat);
      }
    }
    if (engagementGaps.length > 0) {
      sr.engagement_gaps = engagementGaps;
      sendLog(ws, `[FreshReview] Engagement gaps (regressing categories without SR reasoning): ${engagementGaps.join(', ')}.\n`);
    }
  }

  // Augment check_results with FP-rate hints for categories with reliable false-positive data.
  const checkResultsForHints = sr.check_results as Record<string, Record<string, unknown>> | undefined;
  if (checkResultsForHints) {
    augmentCheckResultsWithFpRateHints(checkResultsForHints, projectContext);
  }

  // Also augment reviewer_findings with FP-rate hints for rendering in sr-pr-section.js.
  const srCatchRate = projectContext?.self_review_catch_rate as {
    categories?: Record<string, SRCatchRateCategory>;
  } | null | undefined;
  const fpRateCategories = srCatchRate?.categories ?? {};
  const reviewerFindings = sr.reviewer_findings as Array<Record<string, unknown>> | undefined;
  if (Array.isArray(reviewerFindings)) {
    for (const finding of reviewerFindings) {
      const cat = finding.category as string | undefined;
      if (!cat) continue;
      // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
      const categoryData = fpRateCategories[cat];
      // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
      if (!categoryData) continue;
      const fpRate = categoryData.false_positive_rate ?? 0;
      const sampleSize = categoryData.sr_flag_sample_size ?? 0;
      if (fpRate >= 0.5 && sampleSize >= 10) {
        const fpPercent = Math.round(fpRate * 100);
        finding.fp_rate_hint = `(FP rate ${fpPercent}% in this project, n=${sampleSize})`;
      }
    }
  }

  sr.reviewer_findings_filtered = {
    total_reviewer_findings: findings.length,
    filtered_count: hallucinatedCount,
    sampled_phantom_paths: phantomPaths,
  };

  try {
    fs.writeFileSync(srPath, JSON.stringify(sr, null, 2), { mode: 0o644 });
    sendLog(ws, '[FreshReview] Reviewer findings merged into self-review JSON.\n');
  } catch (e) {
    sendLog(ws, `[FreshReview] Failed to write self-review: ${e instanceof Error ? e.message : String(e)}\n`);
  }

  if (originalTaskId) {
    try {
      await apiPost(`/api/agent/tasks/${originalTaskId}/reviewer-merge`, {
        reviewer_findings: groundedFindings,
        reviewer_edges_considered: Array.isArray(parsed.reviewer_edges_considered) ? parsed.reviewer_edges_considered as Record<string, unknown>[] : [],
      });
      sendLog(ws, '[FreshReview] Findings posted to original task.\n');
    } catch (e) {
      sendLog(ws, `[FreshReview] Failed to post findings to original task: ${e instanceof Error ? e.message : String(e)}\n`);
    }
  }
}

/**
 * Extracts the structured self-review JSON object from the agent's output.
 * Looks for a ```json ... ``` code fence first, then falls back to finding
 * the first `{` that can be parsed as JSON.
 */
export function extractSelfReviewJson(output: string): Record<string, unknown> | null {
  const fenceMatch = output.match(/```json\s*\n?([\s\S]*?)\n?```/);
  if (fenceMatch) {
    try { return JSON.parse(fenceMatch[1].trim()) as Record<string, unknown>; } catch { /* fallthrough */ }
  }
  const start = output.indexOf('{');
  if (start !== -1) {
    const end = output.lastIndexOf('}');
    if (end > start) {
      try { return JSON.parse(output.slice(start, end + 1)) as Record<string, unknown>; } catch { /* ignore */ }
    }
  }
  return null;
}

/**
 * Extracts candidate file paths from text (e.g., "path/to/file.ext", "app/foo.py").
 * Returns a deduplicated set of potential file references mentioned in the text.
 */
function extractCandidateFilePaths(text: string): Set<string> {
  const paths = new Set<string>();
  // Match patterns like: path/to/file.ext, app/foo.py, agent/src/bar.ts
  // Pattern: one or more path segments (word/dash/dot/slash) ending in a file extension
  const filePathRegex = /\b([\w\-./]*[\w\-/]+\.[\w]+)\b/g;
  let match;
  while ((match = filePathRegex.exec(text)) !== null) {
    const path = match[1];
    // Filter to paths that contain at least one slash (is actually a path)
    if (path.includes('/')) {
      paths.add(path);
    }
  }
  return paths;
}

/**
 * Extracts changed file paths from git diff output.
 * Returns a set of file paths that appear in "diff --git a/... b/..." lines.
 */
function extractChangedFilesFromDiff(diff: string): Set<string> {
  const files = new Set<string>();
  const lines = diff.split('\n');
  for (const line of lines) {
    if (line.startsWith('diff --git ')) {
      // Extract the b/ path from: diff --git a/path b/path
      const match = line.match(/\s+b\/(.+)$/);
      if (match) {
        files.add(match[1]);
      }
    }
  }
  return files;
}

/**
 * Returns true if a finding is likely hallucinated: it mentions specific file paths
 * but none of them appear in the set of changed files. Findings with no path
 * references are not considered hallucinated (we can't verify them).
 */
function isHallucinatedFinding(
  finding: { description: string },
  changedFiles: Set<string>,
): boolean {
  const candidatePaths = extractCandidateFilePaths(finding.description);

  // No path references in the finding description → keep it (unverifiable)
  if (candidatePaths.size === 0) {
    return false;
  }

  // At least one mentioned path is in the changed files → keep it (grounded)
  for (const path of candidatePaths) {
    if (changedFiles.has(path)) {
      return false;
    }
  }

  // Finding mentions specific paths but none match the diff → hallucinated
  return true;
}
