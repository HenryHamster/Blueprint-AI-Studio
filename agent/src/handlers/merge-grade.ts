import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, validateBranchName, readHandlerOutput,
  persistAndPostStatus, runClaudeCode, sendHeartbeatSync,
  setCurrentPromptFingerprint, resolveBaseBranch,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import {
  PLAYBOOK_GUIDANCE, FIRST_TASK_GUIDANCE, DEFAULT_TOOL_GUIDANCE, READ_ONLY_BASH_WRITE_NOTE,
  readClaudeMd, buildContextPrefix, buildFileTree, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, formatPlaybook, buildRetryContext,
  computePromptFingerprint, type PlaybookEntry,
} from '../prompt.js';
import { providerAutoInjectsClaudeMd } from '../providers/runners.js';
import { resolveBuildConfig } from '../build-config.js';
import { buildPromptSizeTelemetry, setPromptSizeTelemetry } from '../telemetry.js';
import { repairStructuredOutput, type ParsedValueValidationResult } from './structured-output-repair.js';
import {
  resolveCandidateSha, provisionAuditFetchHelpers, designDocAdvertisement,
  fetchCandidateCommitWithRetry,
} from './audits.js';

// ── Release-merge-grade contract ─────────────────────────────────────────────
//
// The binding artifact a `release_merge_grade` task emits. A backend consumer
// (the blind merge-diff goal-coverage gate in release_qualifier) reads this
// shape VERBATIM, so the keys below are a stable contract — do not rename
// without changing the consumer.
//
//   {
//     "graded_candidate_sha": "<40-hex>",   // forced to the pinned candidate
//     "covers_goal": true,                  // does the merged diff satisfy the goal?
//     "coverage_score": 0.7,                // float [0,1] — 1.0 = fully covered
//     "biggest_gap": "<one non-empty sentence>",  // REQUIRED — single biggest
//                                                 // thing still not done toward the goal
//     "rationale": "<short justification>"
//   }
//
// PURPOSE: an uninvested, independent agent grades whether the MERGED diffs
// actually satisfy the version GOAL. It is BLIND to the strategist's progress
// narrative: the payload never carries it, and the prompt forbids requesting or
// inferring one. The grade is strictly merged-diff vs stated-goal.

// ── Goal-verification redesign: per-category grade ───────────────────────────
//
// The version's ACTIVE acceptance criteria are the merge-grade's CATEGORIES. For
// each, the grader decides met / not_met / partial, CITES the merged-diff
// location(s) that support the verdict, and writes the gap. The backend
// re-normalises this against the authoritative active-criteria rubric (an
// ungraded category, or a `met` with no citation, is forced to `not_met`), so
// the agent only needs to emit its best-effort per-category judgement here.
export type CategoryStatus = 'met' | 'not_met' | 'partial';
export type CitationKind = 'diff' | 'test';

export interface CategoryCitation {
  kind: CitationKind;
  ref: string;
}

// ── blocked_by triage taxonomy (design edcc1e6a §3.4) ───────────────────────
// WHY a category is unmet, so a residual can say so instead of every unmet
// residual looking identical.  Only `unbuilt` may consume campaign rung
// budget; `out_of_scope` and `blocked_external` are terminal for the
// commissioning loop.  An unclassified non-met residual defaults to `unbuilt`
// so existing records do NOT silently leave the budget.
export type BlockedBy = 'unbuilt' | 'out_of_scope' | 'blocked_external';

export const BLOCKED_BY_VALUES: readonly BlockedBy[] = [
  'unbuilt',
  'out_of_scope',
  'blocked_external',
];

const DEFAULT_BLOCKED_BY: BlockedBy = 'unbuilt';

/**
 * Coerce a category verdict's `blocked_by` to a canonical triage value.
 *
 * `met` categories have no blocker → null.  A non-met category whose
 * `blocked_by` is absent or unrecognized defaults to `unbuilt` — the
 * conservative value that keeps the residual inside the campaign rung budget
 * (an unclassified residual must not silently become `out_of_scope` and
 * vanish from the budget).
 */
export function normalizeBlockedBy(raw: unknown, status: CategoryStatus): BlockedBy | null {
  if (status === 'met') return null;
  if (typeof raw === 'string') {
    const value = raw.trim();
    if ((BLOCKED_BY_VALUES as readonly string[]).includes(value)) {
      return value as BlockedBy;
    }
  }
  return DEFAULT_BLOCKED_BY;
}

/**
 * True only for residuals that commission campaign rung work.  `unbuilt`
 * (and the absent default, which normalizes to `unbuilt`) consumes rung
 * budget; the other two triage values are terminal for the commissioning
 * loop.
 */
export function blockedByConsumesRungBudget(blockedBy: BlockedBy | null | undefined): boolean {
  return blockedBy == null || blockedBy === 'unbuilt';
}

export interface CategoryVerdict {
  category_key: string;
  status: CategoryStatus;
  // Why the category is unmet (design edcc1e6a §3.4): `unbuilt` |
  // `out_of_scope` | `blocked_external`.  Always null when status is `met`.
  blocked_by: BlockedBy | null;
  gap: string | null;
  citations: CategoryCitation[];
}

export interface MergeGradeArtifact {
  graded_candidate_sha: string;
  covers_goal: boolean;
  coverage_score: number;
  biggest_gap: string;
  rationale: string;
  // Per-active-category verdicts (empty when no rubric was supplied).
  category_verdicts: CategoryVerdict[];
  // The single goal aspect NO category captures, or null when the categories
  // fully decompose the goal. A non-empty string hard-blocks promotion.
  residual_uncaptured_gap: string | null;
}

/**
 * Clamp an arbitrary value into a coverage score in [0, 1]. Non-finite or
 * non-numeric inputs return null so the caller can derive a fallback.
 */
function clampCoverageScore(raw: unknown): number | null {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return null;
  if (raw < 0) return 0;
  if (raw > 1) return 1;
  return raw;
}

/**
 * Validate the raw LLM output against the RELEASE_MERGE_GRADE contract. When
 * covers_goal=true (goal fully met), `biggest_gap` may be empty or omitted since
 * there is no unmet gap to name. When covers_goal=false or absent, a non-empty
 * `biggest_gap` is required. A goal-coverage signal (`covers_goal` boolean OR
 * `coverage_score` number) must always be present.
 */
export function validateMergeGradeShape(raw: unknown): ParsedValueValidationResult {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'release merge grade output was not a JSON object',
    };
  }
  const obj = raw as Record<string, unknown>;
  const hasOwn = (key: string): boolean => Object.prototype.hasOwnProperty.call(obj, key);

  const rawCoversGoal = typeof obj.covers_goal === 'boolean' ? obj.covers_goal : null;
  const clampedScore = clampCoverageScore(obj.coverage_score);
  const coversGoal = rawCoversGoal !== null
    ? rawCoversGoal
    : (clampedScore !== null ? clampedScore >= 0.5 : false);

  // When covers_goal=true, biggest_gap may be empty/omitted. Otherwise, require it.
  const gapValid = coversGoal
    ? true // goal fully covered; no gap required
    : (hasOwn('biggest_gap') && typeof obj.biggest_gap === 'string' && obj.biggest_gap.trim() !== '');
  if (!gapValid) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: coversGoal
        ? 'internal validation error: gap should not be required when covers_goal=true'
        : 'release merge grade output missed a non-empty biggest_gap when goal is not fully covered',
    };
  }

  const hasCoverageSignal = rawCoversGoal !== null || clampedScore !== null;
  if (!hasCoverageSignal) {
    return {
      ok: false,
      reason: 'parsed_object_schema_mismatch',
      detail: 'release merge grade output carried neither covers_goal nor a numeric coverage_score',
    };
  }
  return { ok: true };
}

/**
 * Coerce an arbitrary parsed citation list into the stable `{kind, ref}[]`
 * shape. Bare strings are read as `diff` refs; an unknown kind falls back to
 * `diff`. Entries without a usable ref are dropped, and the list is capped so a
 * runaway grade cannot bloat the artifact.
 */
function normalizeCitations(raw: unknown): CategoryCitation[] {
  if (!Array.isArray(raw)) return [];
  const out: CategoryCitation[] = [];
  for (const c of raw) {
    let kind: CitationKind = 'diff';
    let ref = '';
    if (c && typeof c === 'object' && !Array.isArray(c)) {
      const o = c as Record<string, unknown>;
      const k = strVal(o.kind).trim().toLowerCase();
      kind = k === 'test' ? 'test' : 'diff';
      ref = strVal(o.ref || o.path || o.location).trim();
    } else if (typeof c === 'string') {
      ref = c.trim();
    }
    if (ref) out.push({ kind, ref: ref.slice(0, 200) });
    if (out.length >= 8) break;
  }
  return out;
}

/**
 * Coerce an arbitrary parsed `category_verdicts` array into the stable
 * `CategoryVerdict[]` shape. Lenient by design: the backend re-normalises
 * against the authoritative active-criteria rubric (ungraded → not_met, `met`
 * without a citation → not_met), so this only preserves the agent's judgement.
 */
export function normalizeCategoryVerdicts(raw: unknown): CategoryVerdict[] {
  if (!Array.isArray(raw)) return [];
  const out: CategoryVerdict[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const o = item as Record<string, unknown>;
    const categoryKey = strVal(o.category_key || o.key).trim();
    if (!categoryKey) continue;
    const statusRaw = strVal(o.status).trim().toLowerCase();
    const status: CategoryStatus = (statusRaw === 'met' || statusRaw === 'partial')
      ? statusRaw
      : 'not_met';
    const gapStr = strVal(o.gap).trim();
    out.push({
      category_key: categoryKey,
      status,
      // blocked_by is required on every non-met verdict; absent/unrecognized
      // values default to `unbuilt` (see normalizeBlockedBy).
      blocked_by: normalizeBlockedBy(o.blocked_by, status),
      gap: gapStr !== '' ? gapStr : null,
      citations: normalizeCitations(o.citations),
    });
  }
  return out;
}

/**
 * Coerce an arbitrary parsed object into the stable RELEASE_MERGE_GRADE
 * contract shape. `graded_candidate_sha` is ALWAYS forced to the pinned
 * candidate (never trusting whatever the LLM wrote). `covers_goal` and
 * `coverage_score` are reconciled so both are always populated and consistent:
 * a missing score derives from the boolean (and vice versa). The per-category
 * verdicts and residual are carried verbatim (coerced) — the backend enforces
 * the citation/rubric rules.
 */
export function normalizeMergeGradeOutput(raw: unknown, candidateSha: string): MergeGradeArtifact {
  const obj = (raw && typeof raw === 'object' && !Array.isArray(raw))
    ? raw as Record<string, unknown>
    : {};

  const rawCoversGoal = typeof obj.covers_goal === 'boolean' ? obj.covers_goal : null;
  const clampedScore = clampCoverageScore(obj.coverage_score);

  // Reconcile the two coverage signals into a consistent pair. Prefer the
  // explicit boolean for covers_goal; otherwise derive it from the score
  // (>= 0.5 covers). Prefer the explicit score; otherwise derive 1.0/0.0 from
  // the boolean. Default to "not covered" when neither is present.
  const coversGoal = rawCoversGoal !== null
    ? rawCoversGoal
    : (clampedScore !== null ? clampedScore >= 0.5 : false);
  const coverageScore = clampedScore !== null
    ? clampedScore
    : (coversGoal ? 1 : 0);

  const residualRaw = strVal(obj.residual_uncaptured_gap || obj.residual).trim();

  return {
    graded_candidate_sha: candidateSha,
    covers_goal: coversGoal,
    coverage_score: coverageScore,
    biggest_gap: strVal(obj.biggest_gap).trim(),
    rationale: strVal(obj.rationale || obj.reason).trim(),
    category_verdicts: normalizeCategoryVerdicts(obj.category_verdicts),
    residual_uncaptured_gap: residualRaw !== '' ? residualRaw : null,
  };
}

/**
 * Render the version goal for the prompt. Accepts either `version_goal` or the
 * legacy `goal` key. Returns '' when neither is present (the prompt then tells
 * the agent it has no goal to grade against, which fails loud downstream).
 */
function resolveVersionGoal(payload: Record<string, unknown>): string {
  return strVal(payload.version_goal).trim() || strVal(payload.goal).trim();
}

/**
 * Render the merged-PR window section. The qualifier passes the set of PRs that
 * landed toward the goal as `merged_prs` (preferred) or `pr_window`. Each entry
 * may be a plain string or an object with number/title/summary fields. Returns
 * a guidance block telling the agent to self-derive the merged set from the
 * diff when nothing was supplied.
 */
function formatMergedPrsSection(payload: Record<string, unknown>): string {
  const raw = Array.isArray(payload.merged_prs)
    ? payload.merged_prs
    : (Array.isArray(payload.pr_window) ? payload.pr_window : []);
  const lines: string[] = [];
  for (const entry of raw) {
    if (typeof entry === 'string') {
      const s = entry.trim();
      if (s) lines.push(`- ${s}`);
      continue;
    }
    if (typeof entry === 'number' && Number.isFinite(entry)) {
      lines.push(`- #${entry}`);
      continue;
    }
    if (entry && typeof entry === 'object') {
      const o = entry as Record<string, unknown>;
      const rawNum = o.number ?? o.pr_number ?? o.id;
      // Accept either a numeric PR number or a string (strVal drops numbers).
      const num = (typeof rawNum === 'number' && Number.isFinite(rawNum))
        ? String(rawNum)
        : strVal(rawNum).trim().replace(/^#/, '');
      const title = strVal(o.title || o.summary || o.description).trim();
      const label = [num ? `#${num}` : '', title].filter(Boolean).join(' ');
      if (label) lines.push(`- ${label}`);
    }
  }
  if (lines.length === 0) {
    return (
      'No explicit merged-PR list was supplied. Derive the merged change set ' +
      'yourself from the diff range above (`git log` / `git diff`).\n\n'
    );
  }
  return (
    'The following pull requests were merged toward this version goal — judge ' +
    'whether, taken together, their changes satisfy the goal:\n' + lines.join('\n') + '\n\n'
  );
}

/**
 * Render the acceptance-criteria CATEGORY rubric (goal-verification redesign).
 * Each active category carries a stable `category_key` the agent MUST echo back
 * in its per-category verdict. Returns '' when no rubric was supplied — the
 * grade is then holistic-only and the backend applies no per-category gate.
 */
function formatCategoriesSection(payload: Record<string, unknown>): string {
  const raw = Array.isArray(payload.categories) ? payload.categories : [];
  const lines: string[] = [];
  for (const entry of raw) {
    if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
      const o = entry as Record<string, unknown>;
      const key = strVal(o.category_key || o.criterion_key || o.key).trim();
      const text = strVal(o.text || o.description).trim();
      if (key) lines.push(`- \`${key}\`: ${text || '(no description)'}`);
    }
  }
  if (lines.length === 0) return '';
  return (
    '## Acceptance-criterion categories (grade EACH against the merged diff)\n' +
    'These are the version\'s acceptance criteria — the CATEGORIES you must grade ' +
    'the merged diff against. For EACH category below, decide `met`, `partial`, or ' +
    '`not_met`, CITE the merged-diff location(s) that justify the verdict, and name ' +
    'the gap when it is not fully met. Echo the exact `category_key` back in each ' +
    'verdict. A category you mark `met` with NO citation is treated as NOT met.\n' +
    lines.join('\n') + '\n\n'
  );
}

// ── Handler: Release Merge Grade ─────────────────────────────────────────────
//
// An uninvested, independent grader of whether the MERGED diffs actually
// satisfy the version GOAL. Like handleReleaseAudit / handleReleaseAuditChallenge
// it clones the base branch with FULL history, fetches the PINNED candidate SHA,
// and fails loud (diff_range_unresolvable) if the candidate / merge-base diff
// range cannot be resolved — an ungraded gate is worse than none. It then grades
// `base...candidate` against the stated goal and emits a BINDING
// RELEASE_MERGE_GRADE artifact naming the single biggest unmet gap. The grader is
// BLIND to the strategist's progress narrative: the payload never carries it and
// the prompt forbids requesting or inferring one.
export async function handleReleaseMergeGrade(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  // CRITICAL: clone base_branch (the reference branch), never the candidate's
  // own branch — the grade measures the incoming merged diff against the goal.
  const rawBaseBranch = resolveBaseBranch(payload, task);
  const baseBranch = validateBranchName(rawBaseBranch, 'base_branch');

  // The grade MUST score one pinned candidate commit (same contract as the
  // release_audit). A missing/malformed candidate_sha is a real contract bug —
  // fail loud rather than diff a non-resolvable range.
  const candidateSha = resolveCandidateSha(payload, task);
  if (!candidateSha) {
    const rawSeen = strVal(payload.candidate_sha) || strVal(
      (task.context && typeof task.context === 'object' && !Array.isArray(task.context)
        ? (task.context as Record<string, unknown>).candidate_sha
        : undefined),
    );
    sendLog(ws, `[ERROR] Release merge grade aborted: no valid candidate_sha in task payload.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'diff_range_unresolvable',
      logs: sanitize(
        `Release merge grade aborted: the task payload carries no valid ` +
        `candidate_sha. The release qualifier must pin the candidate commit by ` +
        `passing a full 40-char commit SHA as payload.candidate_sha. Without it ` +
        `there is no commit to diff against ${baseBranch}. ` +
        `Saw: ${rawSeen ? `"${rawSeen}"` : '(absent)'}`,
      ),
    });
    return;
  }

  const versionGoal = resolveVersionGoal(payload);
  if (!versionGoal) {
    // The backend only spawns a merge-grade child when the goal is non-empty, so
    // a missing goal here is a contract bug — fail loud rather than grade blind.
    sendLog(ws, `[ERROR] Release merge grade aborted: no version_goal in task payload.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'release_merge_grade_no_goal',
      logs: sanitize(
        `Release merge grade aborted: the task payload carries no version_goal. The ` +
        `release qualifier must pass the frozen version goal as payload.version_goal.`,
      ),
    });
    return;
  }

  // Three-dot (merge-base) diff of the pinned candidate vs the base branch.
  const diffRange = `${baseBranch}...${candidateSha}`;

  statusRef.value = `Cloning ${owner}/${repo} (${baseBranch}) for release merge grade...`;
  sendLog(ws, `Starting release merge grade — cloning ${baseBranch}, candidate ${candidateSha.slice(0, 12)}...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // A FULL clone (depth 0) is required: on a long-diverged candidate a shallow
  // clone leaves the merge-base unreachable and the three-dot diff is wrong.
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 0,
    // Merge grade is an independent verification role — never warm-resume onto a
    // lane workspace (gate integrity; lane pilot lane-izes only the fix side).
    allowLaneResume: false,
  });

  // Fetch the EXACT candidate commit so git diff can compute the merge-base
  // diff (do NOT check it out — the working tree stays on baseBranch). A SHA
  // fetch leaves a real commit object behind where a bare-branch fetch only
  // updates FETCH_HEAD. Bounded retry-with-backoff so a transient GitHub I/O
  // blip on this one fetch does not torch the whole qualification attempt;
  // pre-flight-verify the diff range and fail loud below if it stays
  // unresolvable. Mirrors handleReleaseAudit's diff_range_unresolvable guard.
  sendLog(ws, `Fetching candidate commit ${candidateSha.slice(0, 12)} (full history) for diff computation...\n`);
  const { diffRangeResolves, diffRangeFailDetail } =
    await fetchCandidateCommitWithRetry(candidateSha, diffRange, ws);

  if (!diffRangeResolves) {
    sendLog(ws, `Aborting release merge grade: cannot compute ${diffRange} — refusing to emit an ungraded gate signal.\n`);
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status: 'FAILED',
      artifact_type: 'ERROR_LOG',
      failure_category: 'diff_range_unresolvable',
      logs: sanitize(
        `Release merge grade aborted: diff range ${diffRange} does not resolve. ` +
        `The candidate commit ${candidateSha} could not be fetched, or the ` +
        `merge-base of ${baseBranch} and the candidate is unreachable. ` +
        `Detail: ${diffRangeFailDetail || '(no detail)'}`,
      ),
    });
    return;
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const mergeGradeFetchGuidance = provisionAuditFetchHelpers(taskId, task);
  const disallowedTools = configureAgentPermissions('release_merge_grade');

  const contextPrefix = buildContextPrefix(task);
  const claudeMd = readClaudeMd();
  const codebaseSummary = (claudeMd && !providerAutoInjectsClaudeMd()) ? `## Codebase Instructions\n${claudeMd}\n\n` : '';
  const fileTreeSection = buildFileTree(ws);
  const playbookSection = formatPlaybook(task.playbook as Record<string, PlaybookEntry | string> | undefined);
  const retryContext = buildRetryContext(task);
  const firstTaskSection = task.first_task_on_repo ? FIRST_TASK_GUIDANCE : '';

  const goalSection = versionGoal
    ? `## Version goal (grade strictly against this)\n${versionGoal}\n\n`
    : '## Version goal\n(No goal text was supplied in your payload. If the goal is genuinely empty, set covers_goal=false, coverage_score=0, and name the missing goal as the biggest_gap.)\n\n';
  const mergedPrsSection = formatMergedPrsSection(payload);
  const categoriesSection = formatCategoriesSection(payload);
  const hasCategories = categoriesSection !== '';

  const mgCorePrompt = `You are an UNINVESTED, independent release MERGE-GRADER. You did not write any of this code and have no stake in it. \`${candidateSha}\` is the PINNED release candidate commit; it has already been fetched into this clone on \`${baseBranch}\`, so the diff range \`${diffRange}\` resolves. Your single job: judge whether the MERGED diff actually satisfies the version goal below.

BLINDNESS RULE (critical): grade STRICTLY the merged code changes against the stated goal. You have NO access to any strategist progress narrative, status report, or self-description of what was "supposed" to have shipped — and you must NOT request, infer, reconstruct, or rely on one. The only evidence that counts is the actual merged diff versus the goal text. Do not let any narrative claim of progress substitute for code that is actually present in the diff.

${goalSection}${mergedPrsSection}${categoriesSection}## Your task

1. **Obtain the merged diff.** Run \`git diff ${diffRange}\` (and \`git diff --stat ${diffRange}\`, \`git log ${diffRange} --oneline\`) to see exactly what the candidate \`${candidateSha}\` changes relative to \`${baseBranch}\`. Always use the pinned SHA \`${candidateSha}\`, never a bare branch name.

2. **Grade goal coverage.** Decompose the version goal into the concrete things it requires. For each, check the merged diff for code that actually delivers it (read the changed files; do not trust commit messages or PR titles). Decide how much of the goal the merged diff genuinely covers.
${hasCategories ? `
3. **Grade EACH acceptance-criterion category.** For every category listed above, read the merged diff and decide \`met\` / \`partial\` / \`not_met\`. You MUST cite the concrete diff location(s) (file path, ideally file:line, that you actually read) that justify a \`met\` or \`partial\` verdict — an uncited "it looks fine" counts as \`not_met\`. For anything not fully \`met\`, write the specific gap AND classify WHY it is unmet in \`blocked_by\`: \`"unbuilt"\` (the capability genuinely does not exist yet and is buildable within this version's scope — this is the default when you are not sure), \`"out_of_scope"\` (deliberately not part of this version/objective; needs a scope decision, not a build), or \`"blocked_external"\` (real work, but gated on something outside this repo — a deploy, a credential, an upstream dependency). Only \`unbuilt\` residuals commission follow-up build work. Echo the exact \`category_key\` for each. When a specific AUTOMATED TEST in the merged diff actually proves a category is \`met\` (the test exercises the behaviour the criterion requires), ALSO add a \`{"kind": "test", "ref": "<test file path, e.g. backend/testing/test_foo.py, or a pytest node id path::test_name>"}\` citation for that category alongside the diff citation — the backend validates that the cited test really shipped and ran green under CI on this candidate and, if so, upgrades that category to a higher machine-verified tier. Only cite a test that genuinely covers the criterion; do NOT invent or pad test citations.

4. **Answer the residual question.** After grading every category, ask: is there any part of the version goal that NONE of these categories captures? If yes, put that single uncaptured aspect in \`residual_uncaptured_gap\` (a non-empty sentence). If the categories fully decompose the goal, set \`residual_uncaptured_gap\` to null.

5. **Name the single biggest gap.** Identify the ONE biggest thing the goal still requires that the merged diff does NOT deliver. This is required and must be a single, specific, non-empty sentence. If the goal is fully covered, state that the largest remaining risk/gap is minor and name it concretely (still non-empty).

6. **Emit results.** Write your grade as a JSON object to \`/tmp/release_merge_grade_${taskId}.json\` with EXACTLY this structure:

\`\`\`json
{
  "graded_candidate_sha": "${candidateSha}",
  "covers_goal": false,
  "coverage_score": 0.4,
  "biggest_gap": "The goal requires <a concrete capability the goal names>, but the merged diff only adds <the partial piece that is present> — the rest of that capability is not wired up.",
  "rationale": "Read <the changed file(s)>: <why the diff only partially covers the goal>, so under half the goal is delivered.",
  "category_verdicts": [
    {
      "category_key": "<echo the exact key from the category list>",
      "status": "met",
      "blocked_by": null,
      "gap": null,
      "citations": [{"kind": "diff", "ref": "path/to/file.py:42"}, {"kind": "test", "ref": "backend/testing/test_file.py"}]
    },
    {
      "category_key": "<another key>",
      "status": "not_met",
      "blocked_by": "unbuilt",
      "gap": "The merged diff does not add <the thing this criterion requires>.",
      "citations": [{"kind": "diff", "ref": "path/to/other.py"}]
    }
  ],
  "residual_uncaptured_gap": null
}
\`\`\`
` : `
3. **Name the single biggest gap.** Identify the ONE biggest thing the goal still requires that the merged diff does NOT deliver. This is required and must be a single, specific, non-empty sentence. If the goal is fully covered, state that the largest remaining risk/gap is minor and name it concretely (still non-empty).

4. **Emit results.** Write your grade as a JSON object to \`/tmp/release_merge_grade_${taskId}.json\` with EXACTLY this structure:

\`\`\`json
{
  "graded_candidate_sha": "${candidateSha}",
  "covers_goal": false,
  "coverage_score": 0.4,
  "biggest_gap": "The goal requires <a concrete capability the goal names>, but the merged diff only adds <the partial piece that is present> — the rest of that capability is not wired up.",
  "rationale": "Read <the changed file(s)>: <why the diff only partially covers the goal>, so under half the goal is delivered."
}
\`\`\`
`}
Field semantics:
- \`graded_candidate_sha\`: the pinned candidate SHA — use exactly \`${candidateSha}\`.
- \`covers_goal\`: boolean — true only if the merged diff substantively delivers the goal.
- \`coverage_score\`: float in [0, 1] — 0.0 = the merged diff does nothing toward the goal, 1.0 = the goal is fully delivered by the merged diff.
- \`biggest_gap\`: REQUIRED, a single non-empty sentence naming the SINGLE biggest thing the goal still requires that the merged diff does not deliver. Must be specific (cite the missing capability), never empty.
- \`rationale\`: a short justification citing the specific files/changes you read.${hasCategories ? `
- \`category_verdicts\`: REQUIRED array — one entry per acceptance-criterion category above. Each entry: \`category_key\` (echo the exact key), \`status\` ("met" | "partial" | "not_met"), \`blocked_by\` (REQUIRED whenever status is not "met": "unbuilt" | "out_of_scope" | "blocked_external"; null when "met"), \`gap\` (a specific sentence when not fully met, else null), and \`citations\` (array of {"kind": "diff" | "test", "ref": "<file or file:line you actually read; for "test", the test file path or pytest node id>"}). A \`met\` verdict MUST carry at least one citation. Add a \`"kind": "test"\` citation ONLY when a real automated test in the diff proves the criterion — it earns that category the machine-verified tier when the backend confirms the test shipped and ran green under CI on this candidate.
- \`residual_uncaptured_gap\`: a single sentence naming any goal aspect NO category captures, or null when the categories fully decompose the goal.` : ''}

Rules:
- Read the actual merged diff and the relevant files before grading — do NOT speculate.${hasCategories ? '\n- Every `category_verdicts` entry must echo a `category_key` from the list above; do not invent keys. Cite real diff locations you read — uncited "met" is treated as not met.' : ''}
- IMPORTANT: Write ONLY the JSON object to \`/tmp/release_merge_grade_${taskId}.json\` ${READ_ONLY_BASH_WRITE_NOTE} — no markdown, no code fences, no extra text.
- Also output the same JSON object as the final output in your logs.
- Do NOT modify any files in the repository.${designDocAdvertisement(task)}`;

  const buildConfig = resolveBuildConfig(task);
  const promptSuffix = payload.prompt_suffix ? `\n\nAdditional instructions:\n${strVal(payload.prompt_suffix)}` : '';
  const mgSuffixBlock = firstTaskSection + promptSuffix + PLAYBOOK_GUIDANCE + `\n\n${mergeGradeFetchGuidance}${DEFAULT_TOOL_GUIDANCE}`;
  const mergeGradePrompt = `${codebaseSummary}${playbookSection}${retryContext}${contextPrefix}${fileTreeSection}${mgCorePrompt}${mgSuffixBlock}`;
  const mgTelemetry = buildPromptSizeTelemetry(mergeGradePrompt, [
    ['claude_md', codebaseSummary],
    ['playbook', playbookSection],
    ['retry_context', retryContext],
    ['context_prefix', contextPrefix],
    ['file_tree', fileTreeSection],
    ['core_prompt', mgCorePrompt],
    ['suffix', mgSuffixBlock],
  ]);
  setPromptSizeTelemetry(mgTelemetry.prompt_size_bytes, mgTelemetry.prompt_section_count, mgTelemetry.prompt_section_sizes);
  statusRef.value = 'Running release merge grade...';
  const mergeGradeFingerprint = computePromptFingerprint('release_merge_grade', buildConfig);
  setCurrentPromptFingerprint(mergeGradeFingerprint);
  sendHeartbeatSync(taskId, statusRef.value, { prompt_fingerprint: mergeGradeFingerprint });
  const { exitCode, allOutput, lastResultText } = await runClaudeCode(
    mergeGradePrompt,
    ws, statusRef, model, effort, disallowedTools,
  );

  const handlerOutput = readHandlerOutput(taskId, 'release_merge_grade', ws);

  let logs = '';
  let mergeGradeUnparseable = false;
  if (handlerOutput !== null && validateMergeGradeShape(handlerOutput).ok) {
    logs = sanitize(JSON.stringify(normalizeMergeGradeOutput(handlerOutput, candidateSha)));
  } else {
    // Infer coverage state from the invalid output to determine whether biggest_gap is required during repair.
    const handlerObj = (handlerOutput && typeof handlerOutput === 'object' && !Array.isArray(handlerOutput))
      ? handlerOutput as Record<string, unknown>
      : {};
    const rawCoversGoal = typeof handlerObj.covers_goal === 'boolean' ? handlerObj.covers_goal : null;
    const clampedScore = clampCoverageScore(handlerObj.coverage_score);
    const coversGoalInferred = rawCoversGoal !== null
      ? rawCoversGoal
      : (clampedScore !== null ? clampedScore >= 0.5 : false);

    // When covers_goal=true, biggest_gap is optional. Otherwise, require it.
    const requiredKeys = coversGoalInferred ? [] : ['biggest_gap'];
    const gapRequiredText = coversGoalInferred
      ? ' (biggest_gap is not required when the goal is fully covered, but may be included if needed)'
      : ' (one specific sentence naming the single biggest unmet goal requirement)';

    const repaired = await repairStructuredOutput<Record<string, unknown>>({
      mode: 'object',
      requiredKeys,
      first: { exitCode, allOutput, lastResultText },
      reopenPrompt: `Your prior release merge grade response did not end with a parseable JSON grade object.
Output ONLY the JSON object now — with top-level "graded_candidate_sha" (use "${candidateSha}"), "covers_goal" (boolean), "coverage_score" (float 0..1), "biggest_gap"${gapRequiredText}, and a short "rationale". No markdown, no code fences, no prose.`,
      ws,
      statusRef,
      model,
      effort,
      disallowedTools,
      retryStatus: 'Repairing release merge grade JSON...',
      schemaLabel: 'merge_grade',
      validateParsedValue: validateMergeGradeShape,
    });
    if (repaired.value !== null) {
      logs = sanitize(JSON.stringify(normalizeMergeGradeOutput(repaired.value, candidateSha)));
    } else {
      mergeGradeUnparseable = exitCode === 0;
      logs = sanitize(lastResultText || allOutput.slice(-50_000));
    }
  }

  await persistAndPostStatus(taskId, {
    task_id: taskId,
    status: mergeGradeUnparseable || exitCode !== 0 ? 'FAILED' : 'COMPLETED',
    ...(mergeGradeUnparseable
      ? { artifact_type: 'ERROR_LOG', failure_category: 'release_merge_grade_unparseable' }
      : exitCode !== 0
        ? { artifact_type: 'ERROR_LOG', failure_category: 'claude_code_failed' }
        : { artifact_type: 'RELEASE_MERGE_GRADE' }),
    logs,
  });
}
