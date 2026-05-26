import { execFileSync } from 'child_process';
import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sendLog, validateBranchName,
  persistAndPostStatus, git, setupWorkspace, runClaudeCode,
  captureTestBaseline, getProviderRunner, setCurrentPromptFingerprint, sendHeartbeatSync,
  apiPost,
} from '../index.js';
import {
  getTaskTypeHints, DEFAULT_TOOL_GUIDANCE, PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, CODE_CHANGE_HELPER_GUIDANCE,
  getTaskTypeFocusBlock,
  assembleFailureGuidanceBlocks,
  readClaudeMd, buildContextPrefix, buildFileTree, discoverRelatedTests, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext,
  buildAnalysisPrompt, parseAnalysisPlanFiles, parseAnalysisPlanTestFiles, parseAnalysisPlanVerifyFiles, extractModuleInterfaces,
  discoverAffectedConsumers, type PlaybookEntry, computePromptFingerprint,
} from '../prompt.js';
import { resolveBuildConfig } from '../build-config.js';
import { SCOPE_OVERFLOW_THRESHOLDS, MEDIUM_ANALYSIS_PLAN_FILE_LIMIT, DEFAULT_CLONE_DEPTH } from '../constants.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import { runCodeChangeTask } from './code-change.js';
import { formatExecutionEvidenceForSrPrompt, formatHighFpRateLoosenHintForSrPrompt, formatSrCoFailurePairsForSrPrompt, formatSeverityCalibrationHintForSrPrompt, formatPrecisionRecallQuadrantHintForSrPrompt } from '../prompts/self-review.js';

// ── Standard Handler Helper ───────────────────────────────────────────────

/**
 * Computes a heuristic regression-risk score from a git diff when no test
 * baseline file is available. Signals: high source-to-test line ratio and
 * removed exported symbols. Always tags issue text with [heuristic-derived].
 */
export function computeRegressionRiskHeuristic(diff: string): { passed: boolean; issue: string } {
  const lines = diff.split('\n');
  let srcChanged = 0, testChanged = 0, removedExports = 0;
  let currentFile = '';

  for (const line of lines) {
    if (line.startsWith('diff --git ')) {
      currentFile = line.replace(/^diff --git a\/\S+ b\//, '');
      continue;
    }
    if (line.startsWith('--- ') || line.startsWith('+++ ')) continue;
    const isTest =
      /\/(test|spec|__tests__)\//.test(currentFile) ||
      /\.(test|spec)\.(ts|tsx|js|py)$/.test(currentFile) ||
      /(?:^|\/)test_[^/]+\.py$/.test(currentFile);
    if (/^[+-]/.test(line)) {
      if (isTest) testChanged++; else srcChanged++;
    }
    if (line.startsWith('-') &&
        /^-\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+\w/.test(line)) {
      removedExports++;
    }
  }

  const signals: string[] = [];
  if (srcChanged >= 80 && testChanged === 0) signals.push(`+${srcChanged} source lines / 0 test lines`);
  if (removedExports > 0) signals.push(`${removedExports} export(s) removed`);

  const tag = '[heuristic-derived]';
  if (signals.length === 0) {
    return { passed: true, issue: `${tag} no high-risk signals (${srcChanged} src / ${testChanged} test lines)` };
  }
  return { passed: false, issue: `${tag} ${signals.join('; ')}` };
}



const MODELS_PATH = 'backend/app/models.py';
const ALEMBIC_VERSIONS_DIR = 'backend/alembic/versions/';

/**
 * Returns true when changedPaths includes backend/app/models.py but no new
 * alembic migration file was added under backend/alembic/versions/.
 */
export function needsMigrationGeneration(changedPaths: string[]): boolean {
  if (!changedPaths.some(p => p === MODELS_PATH)) return false;
  return !changedPaths.some(p => p.startsWith(ALEMBIC_VERSIONS_DIR) && p.endsWith('.py'));
}

export interface StandardHandlerOpts {
  taskId: string;
  payload: Record<string, unknown>;
  task: Record<string, unknown>;
  ghToken: string;
  owner: string;
  repo: string;
  ws: WebSocket | null;
  statusRef: { value: string };
  model?: string;
  effort?: string;
  branchPrefix: string;
  buildPrompt: (context: { contextPrefix: string; parentCtx: string; rawPrompt: string }) => string;
  commitPrefix: string;
  prTitlePrefix: string;
  noChangesMessage?: string;
  runLintFix?: boolean;
  runTestFix?: boolean;
  captureBaseline?: boolean;
  runSelfReview?: boolean;
  runSmallScopeAnalysis?: boolean;
  lintContext?: string;
  includeTaskTypeHints?: boolean;
  issueReference?: string;
  relatedFiles?: string[];
  runMigrationCheck?: boolean;
}

/**
 * For small-scope tasks, produce a brief note listing the primary target
 * file's top-level exports and any files that appear to import it. This is
 * a lightweight caller/import check — NOT a full analysis pass — meant to
 * catch import/type mismatches and missed caller updates. Returns '' when
 * nothing useful can be derived.
 */
export function buildSmallScopeCallerNote(relatedFiles: string[]): string {
  if (relatedFiles.length === 0) return '';

  const MAX_FILES = 3;
  const targets = relatedFiles.slice(0, MAX_FILES).filter(t => t && !t.includes('..'));
  if (targets.length === 0) return '';

  const lines: string[] = [];
  let anyFindings = false;

  for (let i = 0; i < targets.length; i++) {
    const target = targets[i];
    let content = '';
    try {
      content = execFileSync('cat', [`${WORKSPACE}/${target}`], { encoding: 'utf-8', timeout: 5_000 });
    } catch { continue; }

    const isPy = target.endsWith('.py');
    const re = isPy
      ? /^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)/gm
      : /^export\s+(?:default\s+|async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+([A-Za-z_]\w*)/gm;
    const exports: string[] = [];
    let m: RegExpExecArray | null;
    while ((m = re.exec(content)) !== null && exports.length < 8) {
      if (!m[1].startsWith('_')) exports.push(m[1]);
    }

    const basename = (target.split('/').pop() || '').replace(/\.(ts|tsx|mjs|cjs|js|jsx|py)$/, '');
    let importers: string[] = [];
    if (basename && /^[A-Za-z_][\w-]*$/.test(basename)) {
      try {
        const out = execFileSync(
          'git',
          ['-C', WORKSPACE, 'grep', '-lE', `^\\s*(import|from)\\b.*\\b${basename}\\b`, '--', '*.ts', '*.tsx', '*.js', '*.py'],
          { encoding: 'utf-8', timeout: 10_000 },
        );
        importers = out.split('\n').filter(f => f && !targets.includes(f)).slice(0, 5);
      } catch { /* git grep returns 1 when no matches */ }
    }

    if (exports.length === 0 && importers.length === 0) continue;
    anyFindings = true;
    const label = i === 0 ? 'Primary target' : 'Related file';
    if (exports.length) lines.push(`${label} \`${target}\` exposes: ${exports.join(', ')}.`);
    if (importers.length) lines.push(`Files that reference \`${target}\`: ${importers.map(f => `\`${f}\``).join(', ')}.`);
  }

  if (!anyFindings) return '';
  lines.push('Preserve these exports and update any callers you change.');
  return lines.join('\n');
}

const FRESH_REVIEWER_MIN_LINES = 20;
const FRESH_REVIEWER_MEDIUM_EFFORT_THRESHOLD = 300;
const SENSITIVE_PATHS = [
  'backend/app/auth',
  'backend/app/security',
  'backend/app/routers/agent',
  'backend/app/jwt_revocation',
];

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

const VIEWPORT_INFRA_SKIP_GUIDANCE = `## Viewport gate: infra-skip sentinel

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

type SRCatchRateExample = { severity: string; desc: string };
type SRCatchRateCategory = {
  miss_rate?: number;
  weighted_miss_rate?: number;
  false_positive_rate?: number;
  precision?: number;
  sr_flag_sample_size?: number;
  examples?: SRCatchRateExample[];
  time_to_miss_detect_lone_checker?: boolean;
  engagement_thinning?: boolean;
};

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

function getRegressingCategories(
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

function getThinningCategories(
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


async function runStandardHandler(opts: StandardHandlerOpts): Promise<void> {
  const {
    taskId, payload, task, ghToken, owner, repo, ws, statusRef,
    model, effort, branchPrefix, buildPrompt,
    noChangesMessage = 'No file changes.',
    runTestFix = false,
    captureBaseline = false,
    runSmallScopeAnalysis = false,
    includeTaskTypeHints = true,
  } = opts;
  // commitPrefix, prTitlePrefix, runLintFix, runSelfReview, lintContext,
  // issueReference, relatedFiles, runMigrationCheck are no longer
  // wrapper-driven (Claude owns its own loop via ./check + ./pr).
  // They remain in StandardHandlerOpts so existing call-sites compile but
  // are intentionally ignored here.

  const buildConfig = resolveBuildConfig(task);
  const ctx = (task.context as Record<string, unknown> | undefined) || {};
  const rawBaseBranch = strVal(payload.base_branch) || strVal(ctx.research_branch) || strVal(ctx.original_base) || 'main';
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');
  const promotionTarget = strVal(payload.promotion_target) || undefined;
  const rawTitle = strVal(task.title) || strVal(payload.title) || 'automated changes';
  const taskTitle = rawTitle.replace(/[`$\\!"';&|<>(){}[\]]/g, '');

  const contextPrefix = buildContextPrefix(task);

  const parentCtxObj = (task.context as Record<string, unknown> | undefined)?.parent_context as Record<string, unknown> | undefined;
  const parentCtx = parentCtxObj?.parent_prompt
    ? `## Parent Task Context\nThis task is a subtask of: **${strVal(parentCtxObj.parent_title)}**\nParent goal: ${strVal(parentCtxObj.parent_prompt)}\n\n`
    : '';

  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const taskTypeKey = strVal(payload.type);
  const taskTypeHintsMap = getTaskTypeHints(buildConfig, promotionTarget);
  const taskTypeHint = includeTaskTypeHints && taskTypeHintsMap[taskTypeKey]
    ? `\n\n${taskTypeHintsMap[taskTypeKey].replace(/\{\{BASE_BRANCH\}\}/g, baseBranch)}`
    : '';

  const claudeMd = readClaudeMd();
  const claudeMdPrefix = claudeMd ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';
  const taskTypeFocusBlock = getTaskTypeFocusBlock(taskTypeKey);
  const executionEvidenceSection = formatExecutionEvidenceForSrPrompt();
  const projectCtxForSr = task.project_context as Record<string, unknown> | undefined;
  const srCategories = (projectCtxForSr?.self_review_catch_rate as { categories?: Record<string, { false_positive_rate?: number; precision?: number; sr_flag_sample_size?: number; severity_calibration?: Record<string, unknown>; precision_recall_quadrant?: string; precision_delta?: number; recall_delta?: number }> } | null | undefined)?.categories;
  const loosenHintSection = formatHighFpRateLoosenHintForSrPrompt(srCategories);
  const coFailurePairs = projectCtxForSr?.sr_co_failure_pairs as Array<{ cat_a: string; cat_b: string; lift: number }> | null | undefined;
  const coFailurePairsSection = formatSrCoFailurePairsForSrPrompt(coFailurePairs);
  const severityCalibrationHintSection = formatSeverityCalibrationHintForSrPrompt(srCategories);
  const precisionRecallQuadrantHintSection = formatPrecisionRecallQuadrantHintForSrPrompt(srCategories);
  const finalPrompt = claudeMdPrefix + playbookSection + retryContext + buildPrompt({ contextPrefix, parentCtx, rawPrompt }) + taskTypeHint + firstTaskSection + CODE_CHANGE_HELPER_GUIDANCE + taskTypeFocusBlock + loosenHintSection + coFailurePairsSection + severityCalibrationHintSection + precisionRecallQuadrantHintSection + executionEvidenceSection + PLAYBOOK_GUIDANCE + `\nNote: If lint or test fixes required non-obvious steps, save them to the playbook (e.g., ./playbook set "lint-gotcha" "description").\n` + assembleFailureGuidanceBlocks() + VIEWPORT_INFRA_SKIP_GUIDANCE + `\n\n${DEFAULT_TOOL_GUIDANCE}` + promptSuffix;

  const { workBranch } = await setupWorkspace(owner, repo, ghToken, baseBranch, branchPrefix, taskId, ws, statusRef, buildConfig);
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureAgentPermissions(taskTypeKey);

  // ── SCOPE_EMPTY pre-flight (auto-decomposed cluster siblings) ────────────
  // When all declared related_files were already shipped by completed sibling
  // cluster PRs, exit cleanly rather than burning retries on CODE_ERROR.
  if (ctx.auto_decomposed) {
    const guardFiles = (
      (payload.related_files as string[] | undefined) ||
      (ctx.related_files as string[] | undefined) ||
      []
    );
    if (guardFiles.length > 0) {
      const projectCtxForGuard = task.project_context as Record<string, unknown> | undefined;
      const siblingTasksRaw = projectCtxForGuard?.sibling_tasks as Array<Record<string, unknown>> | undefined;
      if (Array.isArray(siblingTasksRaw)) {
        const completedSiblingFiles = new Set<string>();
        const coveringTitles: string[] = [];
        for (const sibling of siblingTasksRaw) {
          if (sibling.status !== 'COMPLETED') continue;
          const sFiles = sibling.files as string[] | undefined;
          if (!Array.isArray(sFiles)) continue;
          const overlaps = sFiles.filter(f => guardFiles.includes(f));
          if (overlaps.length > 0) {
            for (const f of sFiles) completedSiblingFiles.add(f);
            const rawTitle = sibling.title;
          const rawId = sibling.id;
          const title = typeof rawTitle === 'string' && rawTitle ? rawTitle
            : typeof rawId === 'string' && rawId ? rawId
            : 'sibling task';
            if (!coveringTitles.includes(title)) coveringTitles.push(title);
          }
        }
        if (completedSiblingFiles.size > 0 && guardFiles.every(f => completedSiblingFiles.has(f))) {
          const scopeEmptyMsg = `SCOPE_EMPTY: All declared related_files [${guardFiles.join(', ')}] were already covered by completed sibling tasks (${coveringTitles.join(', ')}). Exiting cleanly — no retry needed.`;
          sendLog(ws, `[ScopeGuard] ${scopeEmptyMsg}\n`);
          await persistAndPostStatus(taskId, {
            task_id: taskId,
            status: 'BLOCKED_PRECONDITION',
            artifact_type: 'ERROR_LOG',
            logs: scopeEmptyMsg,
            failure_category: 'SCOPE_EMPTY',
          });
          return;
        }
      }
    }
  }

  // Capture test baseline on main branch so the test fix loop can distinguish
  // pre-existing failures from newly-broken tests. Scoped to related_files to stay fast.
  //
  // EXCEPTION: auto-remediation tasks deliberately skip baseline capture —
  // their WHOLE JOB is to fix whatever's broken on main, pre-existing or
  // otherwise. A baseline that excludes broken-main tests would make the
  // remediation into a no-op: Claude would see "pre-existing failures, don't
  // touch" and give up. We want it to see all failures and fix them.
  const isAutoRemediation = Boolean(
    (task.context as Record<string, unknown> | undefined)?.auto_remediation
  );
  if (captureBaseline && runTestFix && baseBranch === (promotionTarget ?? 'main') && !isAutoRemediation) {
    const relatedFiles = (
      (payload.related_files as string[] | undefined) ||
      ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
      []
    );
    const baselineFailures = await captureTestBaseline(ws, statusRef, buildConfig, relatedFiles);
    if (baselineFailures !== null) {
      try {
        fs.writeFileSync(
          `/tmp/test_baseline_${taskId}.json`,
          JSON.stringify(Array.from(baselineFailures)),
          { mode: 0o644 },
        );
      } catch (e) {
        sendLog(ws, `[Baseline] Failed to write baseline file: ${e instanceof Error ? e.message : String(e)}\n`);
      }
    }
  } else if (isAutoRemediation) {
    sendLog(ws, '[Baseline] Skipped — this is an auto-remediation task; all failures are fair game.\n');
  }

  const fileTree = buildFileTree(ws);

  // ── Phase 1: Pre-implementation analysis (medium-scope tasks only) ──────────
  // Runs a read-only pass to map module interfaces and dependencies before coding.
  let analysisContext = '';
  const taskScope = strVal(ctx.scope);
  const previousAnalysisPlan = strVal(ctx._previous_analysis_plan);
  if (taskScope === 'medium' && previousAnalysisPlan) {
    // Retry with an existing plan: reuse it instead of burning another ~3-5 min
    // Claude Code session re-deriving the same analysis.
    sendLog(ws, '\n[Analysis Phase] Reusing plan from previous attempt — skipping re-analysis.\n');
    const trimmed = previousAnalysisPlan.trim();
    analysisContext = trimmed.startsWith('## Pre-Implementation Analysis')
      ? `${trimmed}\n\n`
      : `## Pre-Implementation Analysis\n\n${trimmed}\n\n`;
  } else if (taskScope === 'medium') {
    sendLog(ws, '\n[Analysis Phase] Mapping module interfaces and dependencies...\n');
    statusRef.value = 'Analyzing module interfaces...';

    const analysisRelatedFiles = (
      (payload.related_files as string[] | undefined) ||
      (ctx.related_files as string[] | undefined) ||
      []
    );

    const analysisProjectCtx = task.project_context as Record<string, unknown> | undefined;
    const rawConcurrent = analysisProjectCtx?.direction_concurrent_files;
    const directionConcurrentFiles = Array.isArray(rawConcurrent)
      ? (rawConcurrent as unknown[]).filter((f): f is string => typeof f === 'string' && f.length > 0)
      : [];
    const rawCochange = analysisProjectCtx?.cochange_files;
    const cochangeFiles = Array.isArray(rawCochange)
      ? (rawCochange as unknown[]).filter((f): f is string => typeof f === 'string' && f.length > 0)
      : [];

    const diagnosisTaskId = strVal(ctx.diagnosis_task_id);
    const diagnosisContext = diagnosisTaskId
      ? {
          rootCause: strVal(ctx.diagnosis_root_cause),
          recommendedFix: strVal(ctx.diagnosis_recommended_fix),
          priorFailedApproaches: (() => {
            const summary = strVal(ctx._previous_error_summary);
            return summary ? [summary] : [];
          })(),
        }
      : undefined;
    const ctxPlanLimit = ctx._analysis_plan_file_limit;
    const prodFileLimit = (typeof ctxPlanLimit === 'number' && ctxPlanLimit > 0 && ctxPlanLimit <= MEDIUM_ANALYSIS_PLAN_FILE_LIMIT)
      ? Math.floor(ctxPlanLimit)
      : MEDIUM_ANALYSIS_PLAN_FILE_LIMIT;
    if (prodFileLimit !== MEDIUM_ANALYSIS_PLAN_FILE_LIMIT) {
      sendLog(ws, `[Analysis Phase] Using tightened production file limit ${prodFileLimit} (from prior-attempt retry context).\n`);
    }
    const analysisPrompt = buildAnalysisPrompt({
      rawPrompt,
      taskTitle,
      relatedFiles: analysisRelatedFiles,
      fileTree,
      fileLimit: prodFileLimit,
      totalFileBudget: prodFileLimit,
      directionConcurrentFiles,
      cochangeFiles,
      diagnosisContext,
    });

    const { exitCode: analysisExitCode, lastResultText: analysisResult } = await runClaudeCode(
      analysisPrompt,
      ws, statusRef, model, effort,
      ['Write', 'Edit', 'NotebookEdit'],
    );

    if (analysisExitCode === 0 && analysisResult.trim()) {
      let effectiveAnalysis = analysisResult.trim();
      let plannedFiles = parseAnalysisPlanFiles(effectiveAnalysis);
      let plannedTestFiles = parseAnalysisPlanTestFiles(effectiveAnalysis);
      let plannedVerifyFiles = parseAnalysisPlanVerifyFiles(effectiveAnalysis);

      // Scope feasibility gate: if the analysis plans more production files
      // than the medium-scope limit, retry the analysis once with explicit
      // narrowing guidance. If still over budget, fail fast — the
      // implementation pass would otherwise burn ~5-10 minutes only to hit
      // scope_overflow. Test files are deliberately excluded from this budget
      // so coverage can scale with the production change.
      if (plannedFiles.length > prodFileLimit) {
        sendLog(ws, `[Analysis Phase] plan lists ${plannedFiles.length} production files (limit ${prodFileLimit}); retrying with narrowing directive.\n`);
        const testFilesBlock = plannedTestFiles.length > 0
          ? `\nPreviously planned test files (not counted against the budget — keep whatever coverage the change needs):\n${plannedTestFiles.map(f => `- ${f}`).join('\n')}`
          : '';
        const narrowingGuidance = `The previous analysis pass planned ${plannedFiles.length} production files, exceeding the production-file limit of ${prodFileLimit}. Test files do NOT count toward this limit — do not trim test coverage to hit the number. Narrow the **production** plan to the minimum set of files that form a **coherent change** — the files that MUST change together for the feature to work correctly (e.g. a shared type touched in backend + agent is one logical unit and should stay together). Do NOT split a logical unit just to reduce file count; instead, drop files that belong to a *different* concern — peripheral cleanups, nice-to-have refactors, or independent follow-on work that can ship as a separate task without breaking this one. In **Implementation approach**, add a "Deferred" subsection explicitly listing any files/concerns you dropped so the next task can pick them up. Previously planned production files:\n${plannedFiles.map(f => `- ${f}`).join('\n')}${testFilesBlock}`;
        const retryPrompt = buildAnalysisPrompt({
          rawPrompt,
          taskTitle,
          relatedFiles: analysisRelatedFiles,
          fileTree,
          fileLimit: prodFileLimit,
          totalFileBudget: prodFileLimit,
          narrowingGuidance,
          directionConcurrentFiles,
          cochangeFiles,
          diagnosisContext,
        });
        const { exitCode: retryExitCode, lastResultText: retryResult } = await runClaudeCode(
          retryPrompt,
          ws, statusRef, model, effort,
          ['Write', 'Edit', 'NotebookEdit'],
        );
        if (retryExitCode === 0 && retryResult.trim()) {
          effectiveAnalysis = retryResult.trim();
          plannedFiles = parseAnalysisPlanFiles(effectiveAnalysis);
          plannedTestFiles = parseAnalysisPlanTestFiles(effectiveAnalysis);
          plannedVerifyFiles = parseAnalysisPlanVerifyFiles(effectiveAnalysis);
        }

        if (plannedFiles.length > prodFileLimit) {
          const fileList = plannedFiles.map(f => `- ${f}`).join('\n');
          const testList = plannedTestFiles.length > 0
            ? `\n\nPlanned test files (not counted against the budget):\n${plannedTestFiles.map(f => `- ${f}`).join('\n')}`
            : '';
          const failureLog = `Pre-implementation analysis planned ${plannedFiles.length} production files, exceeding the production-file limit of ${prodFileLimit}. Test files do not count toward this limit. The narrowing retry could not bring the plan within budget. Split this task along a logical boundary — separate out any files belonging to a different concern as a follow-on task — rather than forcing a single-file solution that breaks the coherent change set.\n\nPlanned production files:\n${fileList}${testList}`;
          sendLog(ws, `[Analysis Phase] Plan still exceeds production budget after retry (${plannedFiles.length} prod files, limit ${prodFileLimit}) — failing fast.\n`);
          await persistAndPostStatus(taskId, {
            task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
            logs: failureLog,
            failure_category: 'scope_overflow',
            scope_gate_triggered: 'analysis_overflow',
            analysis_plan: effectiveAnalysis.slice(0, 5000),
          });
          return;
        }
        sendLog(ws, `[Analysis Phase] Plan narrowed to ${plannedFiles.length} production files (${plannedTestFiles.length} test files, uncapped; ${plannedVerifyFiles.length} verify-only files, uncapped).\n`);
      }

      analysisContext = `## Pre-Implementation Analysis\n\n${effectiveAnalysis}\n\n`;
      sendLog(ws, '[Analysis Phase] Complete.\n');
    } else {
      sendLog(ws, '[Analysis Phase] Skipped — proceeding to implementation.\n');
    }
  } else if (taskScope === 'small' && runSmallScopeAnalysis) {
    const smallRelated = (
      (payload.related_files as string[] | undefined) ||
      (ctx.related_files as string[] | undefined) ||
      []
    );
    const callerNote = buildSmallScopeCallerNote(smallRelated);
    const callerSection = callerNote ? `\n\n${callerNote}` : '';
    analysisContext = `## Pre-Implementation Analysis\n\nBefore writing code: read the files relevant to this task, confirm the scope stays under ~50 lines across 1–3 files, and check for callers that import from files you will modify.${callerSection}\n\n`;
  }

  const finalPromptWithTree = fileTree + analysisContext + finalPrompt;
  const promptFingerprint = computePromptFingerprint(taskTypeKey, buildConfig);

  statusRef.value = `Running ${getProviderRunner().displayName}...`;
  setCurrentPromptFingerprint(promptFingerprint);
  // Emit once before Claude starts so short code-change tasks persist the
  // fingerprint even if they finish before the next periodic heartbeat.
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });
  // Record HEAD before Claude runs so we can detect commits Claude made (e.g. before rebasing)
  const baseCommit = git(['rev-parse', 'HEAD']);
  // 2026-05-04: wrapper-driven lint/test/push/PR pipeline replaced with
  // runCodeChangeTask. Claude owns its own loop via ./check + ./pr +
  // ./done + ./fail helpers. Wrapper just runs Claude, watches markers,
  // and posts terminal status. See agent/src/handlers/code-change.ts.
  const regressingCats = getRegressingCategories(projectCtxForSr);
  const thinningCats = getThinningCategories(projectCtxForSr);
  const reasoningGateCats = [...new Set([...regressingCats, ...thinningCats])];
  await runCodeChangeTask({
    taskId,
    prompt: finalPromptWithTree,
    ws,
    statusRef,
    workBranch,
    baseBranch,
    owner,
    repo,
    testsOnMainBranch: baseBranch === (promotionTarget ?? 'main'),
    baseCommit,
    model,
    effort,
    disallowedTools,
    noChangesMessage,
    analysisContext,
    regressingCategories: reasoningGateCats.length > 0 ? reasoningGateCats : undefined,
    buildConfig,
  });

  // Fresh reviewer pass: read-only second session to catch what the implementing
  // session missed. Enabled per-project via project_metadata.enable_fresh_reviewer.
  // Skipped for auto-remediation tasks (their job IS to fix what a reviewer would flag).
  const projectMeta = task.project_metadata as Record<string, unknown> | undefined;
  const freshReviewerEnabled =
    projectMeta?.enable_fresh_reviewer === true;
  let freshReviewerAlreadyRan = false;
  if (freshReviewerEnabled && !isAutoRemediation) {
    freshReviewerAlreadyRan = true;
    const reviewerRelatedFiles = (
      (payload.related_files as string[] | undefined) ||
      ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
      []
    );
    await runFreshReviewerPass(
      taskId,
      taskTitle,
      rawPrompt,
      reviewerRelatedFiles,
      ws,
      statusRef,
      model,
      task.project_context as Record<string, unknown> | undefined,
    );
  }

  // Auto-trigger fresh-reviewer when lone-checker categories overlap this task's
  // risk surface. SR is the sole corroborator for these categories, so a PASS is
  // uncorroborated. Fires regardless of enable_fresh_reviewer.
  if (!isAutoRemediation && !freshReviewerAlreadyRan) {
    const loneCheckerRelatedFiles = (
      (payload.related_files as string[] | undefined) ||
      ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
      []
    );
    const loneCheckerCats = loneCheckerCategoriesForTask(
      task.project_context as Record<string, unknown> | undefined,
      loneCheckerRelatedFiles,
    );
    if (loneCheckerCats.length > 0) {
      sendLog(ws, `[FreshReview] Lone-checker categories detected (${loneCheckerCats.join(', ')}) — auto-triggering fresh-reviewer pass.\n`);
      freshReviewerAlreadyRan = true;
      await runFreshReviewerPass(
        taskId, taskTitle, rawPrompt, loneCheckerRelatedFiles, ws, statusRef, model,
        task.project_context as Record<string, unknown> | undefined,
        false, true,
      );
    }
  }

  // Auto-trigger fresh-reviewer recovery when SR admits incomplete_review and
  // the normal fresh-reviewer path hasn't already run. Fires regardless of
  // project_metadata.enable_fresh_reviewer. Skipped for auto-remediation tasks
  // (same exclusion as the normal path).
  if (!isAutoRemediation && !freshReviewerAlreadyRan) {
    const srRecoveryPath = `/tmp/self_review_${taskId}.json`;
    let srRecovery: Record<string, unknown> = {};
    try {
      const raw = fs.readFileSync(srRecoveryPath, 'utf-8');
      if (raw) srRecovery = JSON.parse(raw) as Record<string, unknown>;
    } catch { /* may not exist */ }
    const srCategories = Array.isArray(srRecovery.categories) ? srRecovery.categories as string[] : [];
    const existingFindings = srRecovery.reviewer_findings;
    const hasExistingFindings = Array.isArray(existingFindings) && existingFindings.length > 0;
    if (srCategories.includes('incomplete_review') && !hasExistingFindings) {
      sendLog(ws, '[FreshReview] SR reports incomplete_review — auto-triggering recovery pass.\n');
      const recoveryRelatedFiles = (
        (payload.related_files as string[] | undefined) ||
        ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
        []
      );
      await runFreshReviewerPass(
        taskId,
        taskTitle,
        rawPrompt,
        recoveryRelatedFiles,
        ws,
        statusRef,
        model,
        task.project_context as Record<string, unknown> | undefined,
        true,
      );
    }
  }
}

// ── Handler: Create (default — make changes, push, open PR) ─────────────────
export async function handleCreate(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  // Include title as context so the agent understands intent even if the prompt is terse
  const taskTitleStr = strVal(task.title);
  const prompt = taskTitleStr ? `Task: ${taskTitleStr}\n\n${rawPrompt}` : rawPrompt;
  if (!prompt) throw new Error('No prompt found in task payloads');

  const taskCtx = task.context as Record<string, unknown> | undefined;
  const issueNumber = taskCtx?.issue_number as number | undefined;
  const issueUrl = taskCtx?.issue_url as string | undefined;
  const issueReference = issueNumber != null
    ? `Closes ${issueUrl || `#${issueNumber}`}`
    : undefined;

  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const siblingTasks = projectCtx?.sibling_tasks;
  const siblingNote = Array.isArray(siblingTasks) && siblingTasks.length > 0
    ? '\n\nNote: Check the Sibling Tasks listed above and avoid making changes that overlap with or duplicate files they are already working on.'
    : '';

  const overlapWarnings = (task.context as Record<string, unknown> | undefined)?.file_overlap_warnings as Array<{shared_files: string[], with_task: string}> | undefined;
  const overlapWarningText = overlapWarnings && overlapWarnings.length > 0
    ? overlapWarnings.map(w =>
        `WARNING: This task shares files (${w.shared_files.join(', ')}) with parallel task "${w.with_task}". Coordinate carefully or check if that task already addressed this area.`
      ).join('\n') + '\n\n'
    : '';

  const relatedFiles = (
    (payload.related_files as string[] | undefined) ||
    ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
    []
  );

  // Operator-requested fresh-reviewer: clone the original PR branch, run the pass, and return.
  const operatorReviewFor = taskCtx?.operator_requested_review_for as string | undefined;
  if (operatorReviewFor) {
    const prBranch = validateBranchName(strVal(payload.branch), 'branch');
    const prBaseBranch = validateBranchName(strVal(payload.base_branch, 'main'), 'base_branch');
    const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;

    // Check the branch still exists before cloning — auto-delete-on-merge removes
    // the head branch as soon as the PR is merged, making clone fatal. This is a
    // permanent state (not a transient error), so short-circuit cleanly.
    let branchExists = false;
    try {
      const lsOut = execFileSync('git', ['ls-remote', '--heads', cloneUrl, prBranch], { timeout: 30000 });
      branchExists = lsOut.toString().trim().length > 0;
    } catch {
      // ls-remote itself failed (auth error, network, etc.) — let the clone attempt
      // proceed and fail loudly so non-branch errors still surface.
      branchExists = true;
    }
    if (!branchExists) {
      const skipMsg = `PR head branch '${prBranch}' no longer exists on ${owner}/${repo} — likely merged and auto-deleted. Skipping review (nothing to review).`;
      sendLog(ws, `[FreshReview] ${skipMsg}\n`);
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: 'COMPLETED',
        artifact_type: 'ERROR_LOG',
        logs: skipMsg,
        failure_category: 'PR_BRANCH_DELETED',
      });
      return;
    }

    sendLog(ws, `[FreshReview] Operator-requested pass — cloning ${owner}/${repo} branch ${prBranch}\n`);
    await prepareWorkspaceForRole({ workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken, branch: prBranch, baseBranch: prBaseBranch, depth: DEFAULT_CLONE_DEPTH });
    try { git(['fetch', 'origin', `+refs/heads/${prBaseBranch}:refs/remotes/origin/${prBaseBranch}`, '--depth', String(DEFAULT_CLONE_DEPTH)]); } catch { /* ok */ }
    try {
      const mergeBase = git(['merge-base', 'HEAD', `origin/${prBaseBranch}`]).trim();
      if (mergeBase) fs.writeFileSync(`/tmp/base_commit_${taskId}`, mergeBase);
    } catch { /* ok */ }
    const title = strVal(task.title) || 'Operator-requested fresh review';
    await runFreshReviewerPass(taskId, title, rawPrompt || '', relatedFiles, ws, statusRef, model, task.project_context as Record<string, unknown> | undefined, false, false, true, operatorReviewFor);
    return;
  }

  const createBuildConfig = resolveBuildConfig(task);
  const relatedTestContext = discoverRelatedTests(ws, relatedFiles, createBuildConfig);
  const createScope = taskCtx?.scope as string | undefined;
  const isMediumOrLargeScope = createScope === 'medium' || createScope === 'large';
  const moduleInterfaceContext = isMediumOrLargeScope
    ? extractModuleInterfaces(ws, relatedFiles)
    : '';
  const affectedConsumersContext = isMediumOrLargeScope
    ? discoverAffectedConsumers(ws, relatedFiles)
    : '';
  const mediumScopePlanningNote = isMediumOrLargeScope
    ? '\n\n## Pre-Execution Planning\n\nBefore writing any code: (1) Identify which files need to change and why. (2) Consider dependency order — make foundational changes first (e.g., models before services, shared types before consumers). (3) Run the relevant test suite to establish a baseline. (4) Execute changes methodically, verifying each step builds/compiles before moving on.\n'
    : '';
  const createBudget = createScope ? SCOPE_OVERFLOW_THRESHOLDS[createScope] : undefined;
  const createScopeBudgetNote = createBudget
    ? `\n\n## Scope File Budget\n\nYour scope budget allows at most ${createBudget} non-test production files. If your changes would exceed this, focus on the single most critical subset and note what was deferred.\n`
    : '';

  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'task',
    buildPrompt: ({ contextPrefix, parentCtx }) => `${contextPrefix}${parentCtx}${moduleInterfaceContext}${affectedConsumersContext}${relatedTestContext}${overlapWarningText}${mediumScopePlanningNote}${createScopeBudgetNote}${prompt}${siblingNote}`,
    commitPrefix: 'blueprint',
    prTitlePrefix: '[Blueprint]',
    noChangesMessage: 'No file changes.',
    runLintFix: true,
    runTestFix: true,
    captureBaseline: true,
    runSelfReview: true,
    runSmallScopeAnalysis: true,
    lintContext: 'the code you just wrote',
    includeTaskTypeHints: true,
    issueReference,
    relatedFiles,
    runMigrationCheck: true,
  });
}

// ── Handler: Refactor (improve code quality without changing behavior) ────────
export async function handleRefactor(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const rawPrompt = strVal(payload.prompt) || strVal(payload.description) || strVal(task.description);
  if (!rawPrompt) throw new Error('No prompt found in task payloads');

  const refactorOverlapWarnings = (task.context as Record<string, unknown> | undefined)?.file_overlap_warnings as Array<{shared_files: string[], with_task: string}> | undefined;
  const refactorOverlapWarningText = refactorOverlapWarnings && refactorOverlapWarnings.length > 0
    ? refactorOverlapWarnings.map(w =>
        `WARNING: This task shares files (${w.shared_files.join(', ')}) with parallel task "${w.with_task}". Coordinate carefully or check if that task already addressed this area.`
      ).join('\n') + '\n\n'
    : '';

  const refactorRelatedFiles = (
    (payload.related_files as string[] | undefined) ||
    ((task.context as Record<string, unknown> | undefined)?.related_files as string[] | undefined) ||
    []
  );
  const refactorBuildConfig = resolveBuildConfig(task);
  const refactorRelatedTestContext = discoverRelatedTests(ws, refactorRelatedFiles, refactorBuildConfig);
  const refactorScope = strVal((task.context as Record<string, unknown> | undefined)?.scope);
  const refactorModuleInterfaceContext = (refactorScope === 'medium' || refactorScope === 'large')
    ? extractModuleInterfaces(ws, refactorRelatedFiles)
    : '';
  const refactorAffectedConsumersContext = (refactorScope === 'medium' || refactorScope === 'large')
    ? discoverAffectedConsumers(ws, refactorRelatedFiles)
    : '';
  const refactorBudget = refactorScope ? SCOPE_OVERFLOW_THRESHOLDS[refactorScope] : undefined;
  const refactorScopeBudgetNote = refactorBudget
    ? `\n\n## Scope File Budget\n\nYour scope budget allows at most ${refactorBudget} non-test production files. If your changes would exceed this, focus on the single most critical subset and note what was deferred.\n`
    : '';

  const refactorTitle = strVal(task.title, 'Refactor');
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'refactor',
    buildPrompt: ({ contextPrefix, rawPrompt: rp }) =>
      `${contextPrefix}${refactorModuleInterfaceContext}${refactorAffectedConsumersContext}${refactorRelatedTestContext}${refactorOverlapWarningText}${refactorScopeBudgetNote}REFACTOR TASK — STRICT CONSTRAINTS:
- Do NOT add new features or new behavior.
- Do NOT change any observable behavior, API contracts, or public interfaces.
- Focus on ONE refactor concern per task (e.g. extract functions, reduce duplication, improve naming, simplify logic, or remove dead code — pick the most impactful single concern).
- Run the existing test suite BEFORE making any changes to establish a baseline. If tests fail before you start, report them and do not proceed.
- Make your refactor changes.
- Run the test suite AGAIN after your changes. All tests that passed before must still pass. If any test regresses, revert or fix the regression before committing.
- Keep the diff minimal — only change what is necessary for the chosen refactor concern.

Task: ${refactorTitle}

${rp}`,
    commitPrefix: 'refactor',
    prTitlePrefix: 'refactor:',
    noChangesMessage: 'No file changes.',
    runLintFix: true,
    runTestFix: true,
    captureBaseline: true,
    runSelfReview: true,
    lintContext: 'the refactored code',
    runMigrationCheck: true,
  });
}

// ── Handler: Docs (analyze codebase, generate/update documentation, open PR) ─
export async function handleDocs(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'docs',
    buildPrompt: () => `You are a documentation agent. Your job is to improve the documentation of this codebase.

Steps:
1. Analyze the codebase to understand what it does — read the README, key source files, API routes, configuration, and entry points.
2. Identify documentation gaps:
   - Missing or outdated README sections (installation, usage, architecture, configuration)
   - Undocumented public functions, classes, or modules (missing docstrings)
   - Undocumented API endpoints
   - Missing setup or deployment instructions
3. Generate or update documentation to fill those gaps:
   - Improve the README with accurate installation, usage, and architecture sections
   - Add or update docstrings for public functions and classes
   - Create or update API documentation where applicable
4. Focus on accuracy — only document what actually exists in the code. Do not invent features or describe things that don't exist.
5. Do NOT change any production code. Only modify documentation files (README, docs/, etc.) and add/update docstrings within existing source files.

Be thorough but precise. Every statement in the documentation must reflect the actual code.`,
    commitPrefix: 'docs',
    prTitlePrefix: 'docs:',
    noChangesMessage: 'No documentation changes needed.',
    runLintFix: false,
    runTestFix: false,
  });
}

// ── Handler: QA (analyze coverage, generate tests, open PR) ─────────────────
export async function handleQA(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'task',
    buildPrompt: ({ contextPrefix, parentCtx, rawPrompt }) =>
      `${contextPrefix}${parentCtx}You are a QA agent. Your job is to improve test coverage for this codebase.

${rawPrompt ? `Task context:\n${rawPrompt}\n\n` : ''}Instructions:
1. Analyze the existing test files to understand the test framework, patterns, and conventions used.
2. Identify functions, modules, or code paths that have no or insufficient test coverage.
3. Write comprehensive test files (unit tests and/or integration tests) following the exact same patterns and conventions as the existing tests.
4. Only add new test files or append tests to existing test files — do NOT modify any production code.
5. After writing the tests, run them to verify they pass. Fix any failures before finishing.

IMPORTANT RULES:
- Follow the existing test framework and patterns exactly (same imports, same assertion style, same file naming)
- Do not change production source files under any circumstances
- Each test should be self-contained and not depend on external state
- Cover edge cases and error paths, not just happy paths
- If a test requires mocking, use the same mocking approach as existing tests`,
    commitPrefix: 'test',
    prTitlePrefix: 'test: add test coverage for',
    noChangesMessage: 'No test files added.',
    runLintFix: true,
    runTestFix: false,
    runSelfReview: true,
  });
}

// ── Handler: Deps (update dependencies to latest minor/patch versions) ─────
export async function handleDeps(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  await runStandardHandler({
    taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort,
    branchPrefix: 'deps',
    buildPrompt: () => `You are a dependency update agent. Your job is to update this project's dependencies to their latest MINOR and PATCH versions — do NOT make major version bumps as those can introduce breaking changes.

Steps to follow:
1. Detect which package managers are in use by checking for these files:
   - package.json \u2192 npm/yarn/pnpm
   - requirements.txt \u2192 pip
   - pyproject.toml \u2192 poetry or pip
   - Cargo.toml \u2192 cargo
   - go.mod \u2192 go mod
   - Gemfile \u2192 bundler

2. For each package manager found, check what's outdated:
   - npm: run \`npm outdated\` (or \`yarn outdated\` / \`pnpm outdated\`)
   - pip: run \`pip list --outdated\`
   - poetry: run \`poetry show --outdated\`
   - cargo: run \`cargo outdated\`
   - go: run \`go list -u -m all\`

3. Update dependencies to their latest MINOR/PATCH versions only:
   - npm: run \`npm update\` (respects semver ranges) or update package.json version ranges then \`npm install\`
   - pip: update requirements.txt with new versions
   - poetry: run \`poetry update\`
   - cargo: run \`cargo update\`
   - go: run \`go get -u ./...\` (patch only) then \`go mod tidy\`

4. Run the project's test suite to verify nothing broke:
   - Look for test scripts in package.json (e.g. \`npm test\`, \`npm run test\`)
   - Or pytest, cargo test, go test, etc.
   - If tests fail, identify which dependency update caused the failure and revert just that package to its previous version, then re-run tests.

5. Once tests pass, run \`./check\` to confirm lint/build, then \`./pr open --title "deps: ..." --body "Summary of bumps"\` to ship and \`./done\`.

IMPORTANT: Only update minor/patch versions. If a package would require a major version bump to get any update, skip it. Focus on security patches and bug fixes.`,
    commitPrefix: 'chore',
    prTitlePrefix: 'chore:',
    noChangesMessage: 'No dependency updates needed.',
    runLintFix: false,
    runTestFix: false,
  });
}
