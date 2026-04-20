import type { BuildConfig } from '../index.js';
import { getMandatoryChecks } from './analysis.js';

const MIGRATION_COMPLETENESS_ITEM = `**Migration completeness (CRITICAL — HARD GATE, verdict MUST be FAIL if violated)** — \`backend/app/models.py\` is in your diff. Verify all three: (a) a new migration file exists in \`backend/alembic/versions/\` that reflects the schema change; (b) \`EXPECTED_SCHEMA_REVISION\` in \`backend/app/main.py\` was bumped to the new migration's revision id; (c) the new migration's \`down_revision\` chains from the previously-latest migration. If any is missing, create the missing migration (and bump EXPECTED_SCHEMA_REVISION) before re-checking — do not mark passed.`;

function buildSelfReviewChecklist(taskScope?: string, hasAnalysisPlan?: boolean, modelsChanged?: boolean): string {
  const migrationItem = modelsChanged ? `\n7. ${MIGRATION_COMPLETENESS_ITEM}` : '';
  if (taskScope === 'small') {
    return `Severity guide: CRITICAL = data loss / security breach / crash; MAJOR = incorrect behavior / missing coverage; MINOR = style / naming (skip).

Check for:
1. **Correctness** (MAJOR/CRITICAL) — Off-by-one errors, null/undefined handling, edge cases in the changed logic?
2. **Missing test coverage** (MAJOR) — Is there new behavior with no corresponding test?
3. **Error handling** (MAJOR) — Are async operations properly awaited, exceptions caught where needed, null checks present?
4. **Security quick-check** (CRITICAL) — Are any user inputs passed unsanitized to queries, commands, or HTML output?
5. **Import and type verification** (MAJOR) — Are new imports available, type annotations correct, and do modified exports match their callers?
6. **Scope violations** (MAJOR) — Small-scope tasks have a 6-file budget for non-test production files and fail a hard gate if exceeded. Count the non-test files you changed; if over budget or any file is not essential to the task, revert it now.${migrationItem}`;
  }
  if (taskScope === 'large') {
    return `Check for:
1. **Missing test coverage** — Is there new behavior with no corresponding test?
2. **Type safety violations** — TypeScript errors, incorrect type assertions, or missing type guards?
3. **Scope violations** — Are there changes unrelated to the task requirements?
4. **Security issues** — Command injection, SQL injection, XSS, exposed secrets or tokens?
5. **Pattern violations** — Does the code violate the patterns listed in Review Failure Patterns above?
6. **Cross-file consistency** — Do interfaces, types, and exports stay consistent across all changed files?
7. **Backwards compatibility** — Are any existing callers broken by signature or behavior changes?
8. **Data model safety** — Are schema changes (DB, JSONB, API response shapes) backwards-compatible?
9. **Import/dependency verification** — Are new imports available and correctly referenced?${modelsChanged ? `\n10. ${MIGRATION_COMPLETENESS_ITEM}` : ''}`;
  }
  // medium or unspecified: 8-item checklist (includes cross-file and import checks for multi-file changes)
  const planAlignmentItem = hasAnalysisPlan
    ? `0. **Plan alignment (classify unplanned files; HARD GATE only for scope creep, missing planned files, or unverified callers with changed interfaces)** — Tri-directional check: (a) For each non-test file you changed that is NOT in the analysis plan's "Files to change" list, classify it as one of: (a) **caller impact** — required because a changed interface, signature, or type forces the caller to update; (b) **test coverage** — test file paired with a changed production file; (c) **discovered dependency** — shared code or import path that must change for consistency with the planned change; (d) **scope creep** — genuinely unrelated change not required by the planned work. Categories (a)–(c) are acceptable: keep them and note the classification in the \`issue\` field of \`plan_alignment\` so the automated reviewer can evaluate them (this is NOT a FAIL by itself). Category (d) MUST result in a FAIL verdict — revert it. (b) Were all planned production files actually changed, and were files listed under "Tests to add or update" actually created or modified? See "Plan Completion Status" above — any missing planned file MUST result in a FAIL verdict; complete the planned change. Were any interfaces marked "preserve" altered? Altering a preserved interface MUST result in FAIL. (c) **Callers verification** — If the analysis plan contains a "Callers to verify after implementation" section, for each file listed there confirm one of: (i) the interface/symbol it imports was NOT changed, so the caller needs no update (leave unchanged); or (ii) the interface WAS changed and the caller was updated accordingly (should appear in your diff as caller impact). A caller that imports a changed signature but was NOT updated MUST result in a FAIL verdict for cross-file consistency — update the caller before re-checking.\n`
    : '';
  const scopeViolationsItem = hasAnalysisPlan
    ? `3. **Scope violations** — Only a changed non-test file classified as scope creep in the Plan alignment check above MUST result in FAIL. Unplanned files classified as caller impact, test coverage, or discovered dependency are not scope violations — leave them in place and rely on the plan alignment classification.`
    : `3. **Scope violations** — Are there changes unrelated to the task requirements?`;
  return `This is a multi-file change — verify that types, imports, and function signatures are consistent across all changed files.

Check for:
${planAlignmentItem}1. **Missing test coverage** — Is there new behavior with no corresponding test?
2. **Type safety violations** — TypeScript errors, incorrect type assertions, or missing type guards?
${scopeViolationsItem}
4. **Security issues** — Command injection, SQL injection, XSS, exposed secrets or tokens?
5. **Pattern violations** — Does the code violate the patterns listed in Review Failure Patterns above?
6. **Cross-file consistency** — Do interfaces, types, and exports stay consistent across all changed files?
7. **Backwards compatibility** — Are any existing callers broken by signature or behavior changes?
8. **Import/dependency verification** — Are new imports available and correctly referenced?${modelsChanged ? `\n9. ${MIGRATION_COMPLETENESS_ITEM}` : ''}`;
}

// ── Self-review prompt builder ───────────────────────────────────────────────
/**
 * Builds a focused self-review prompt for the agent to check its own diff
 * before committing.  Concentrates only on CRITICAL and MAJOR issues.
 */
export function buildSelfReviewPrompt(opts: {
  diff: string;
  reviewPlaybook: Record<string, string>;
  claudeMd: string;
  buildConfig?: BuildConfig;
  changedFiles?: string[];
  reviewIssueDistribution?: Record<string, number>;
  taskScope?: string;
  scopeOverflowWarning?: string;
  firstAttemptByCategory?: Record<string, number>;
  directionFirstAttemptFeedback?: Record<string, number>;
  persistentReviewIssues?: string[];
  reviewEffectiveness?: { categories?: Record<string, { trend?: string }> };
  analysisPlan?: string;
  planCompletion?: {
    plannedProductionCount: number;
    changedPlannedProductionCount: number;
    plannedTestCount: number;
    changedPlannedTestCount: number;
    missingProduction: string[];
    missingTest: string[];
  };
  topFirstReviewIssues?: Array<{ description: string; count: number; category: string }>;
  moduleFeedbackWarnings?: Array<{ file: string; top_categories: Record<string, number>; sample_size: number }>;
  scopeSpecificReviewIssues?: Array<{ description: string; count: number; category: string; scope: string }>;
  remediationContext?: {
    diagnosisTaskId?: string;
    rootCause?: string;
    recommendedFix?: string;
    severity?: string;
    confidence?: string;
  };
}): string {
  const restrictionsSection = opts.claudeMd
    ? `\n## File Restrictions (from CLAUDE.md)\n${opts.claudeMd.slice(0, 500)}\n`
    : '';

  const reviewIssueDist = opts.reviewIssueDistribution;
  const reviewEffCats = opts.reviewEffectiveness?.categories;
  const trendIndicatorFor = (cat: string): string => {
    const trend = reviewEffCats?.[cat]?.trend;
    return trend === 'increasing' ? ', trending up ⚠'
      : trend === 'decreasing' ? ', trending down'
      : trend === 'stable' ? ', persistent'
      : '';
  };
  const issueDistSection = reviewIssueDist && Object.keys(reviewIssueDist).length > 0
    ? (() => {
        const sorted = Object.entries(reviewIssueDist)
          .sort(([, a], [, b]) => b - a)
          .slice(0, 5)
          .map(([cat, count]) => `${cat} (${Math.round(count * 100)}%${trendIndicatorFor(cat)})`)
          .join(', ');
        const trendingUp = reviewEffCats
          ? Object.entries(reviewEffCats)
              .filter(([, v]) => v?.trend === 'increasing')
              .map(([cat]) => cat)
          : [];
        const warning = trendingUp.length > 0
          ? ` Categories trending up (${trendingUp.join(', ')}) are getting WORSE over recent review cycles — apply extra scrutiny to these before submitting.`
          : '';
        return `\n## Project-Specific Review Patterns\nIn this project, the most common review issues are: ${sorted}. Prioritize these checks.${warning}\n`;
      })()
    : '';

  const firstAttemptByCategory = opts.firstAttemptByCategory;
  const firstAttemptSection = firstAttemptByCategory && Object.keys(firstAttemptByCategory).length > 0
    ? (() => {
        const persistent = new Set(opts.persistentReviewIssues ?? []);
        const lines = Object.entries(firstAttemptByCategory)
          .sort(([, a], [, b]) => b - a)
          .map(([cat, rate]) => {
            const pct = Math.round(rate * 100);
            const tag = persistent.has(cat) ? ' [PERSISTENT]' : '';
            return `- ${cat}: ${pct}% of first-attempt rejections${tag}`;
          })
          .join('\n');
        return `\n## First-Attempt Rejection Priorities\nThese categories most often block first-attempt approval — weight your checks accordingly:\n${lines}\n`;
      })()
    : '';

  const directionFirstAttemptFeedback = opts.directionFirstAttemptFeedback;
  const directionFeedbackSection = directionFirstAttemptFeedback && Object.keys(directionFirstAttemptFeedback).length > 0
    ? (() => {
        const sorted = Object.entries(directionFirstAttemptFeedback)
          .sort(([, a], [, b]) => b - a);
        const summary = sorted.map(([cat, n]) => `${cat}: ${n} rejections`).join(', ');
        const topCat = sorted[0][0];
        return `\n## Direction-Specific First-Attempt Rejections\nFor this direction, the most common first-attempt rejection categories are: ${summary} — weight your checks accordingly and pay extra attention to ${topCat} before submitting.\n`;
      })()
    : '';

  const scopeSpecificReviewIssues = opts.scopeSpecificReviewIssues;
  const scopeSpecificSection = scopeSpecificReviewIssues && scopeSpecificReviewIssues.length > 0
    ? (() => {
        const scope = scopeSpecificReviewIssues[0].scope;
        const lines = scopeSpecificReviewIssues
          .slice(0, 5)
          .map(i => `- "${i.description}" (seen ${i.count}x, category: ${i.category})`)
          .join('\n');
        return `\n## Scope-Specific Review Patterns\nFor ${scope}-scope tasks in this project, these issues are most common:\n${lines}\n`;
      })()
    : '';

  const moduleFeedbackWarnings = opts.moduleFeedbackWarnings;
  const moduleFeedbackSection = moduleFeedbackWarnings && moduleFeedbackWarnings.length > 0
    ? (() => {
        const lines = moduleFeedbackWarnings.map(w => {
          const cats = Object.entries(w.top_categories)
            .sort(([, a], [, b]) => b - a)
            .map(([cat, pct]) => `${Math.round(pct * 100)}% ${cat}`)
            .join(', ');
          return `- \`${w.file}\`: ${cats} (from ${w.sample_size} past issues)`;
        }).join('\n');
        return `\n## File-Specific Review History (files you changed)\nYou modified files with known review patterns. Verify your changes address these:\n${lines}\n`;
      })()
    : '';

  const topFirstReviewIssues = opts.topFirstReviewIssues;
  const topIssuesSection = topFirstReviewIssues && topFirstReviewIssues.length > 0
    ? (() => {
        const lines = topFirstReviewIssues
          .slice(0, 5)
          .map(i => `- "${i.description}" (seen ${i.count}x, category: ${i.category})`)
          .join('\n');
        return `\n## Specific Patterns That Cause First-Review Rejections\nCheck your diff for these:\n${lines}\n`;
      })()
    : '';

  const playbookSection = Object.keys(opts.reviewPlaybook).length > 0
    ? `\n## Review Failure Patterns\nAvoid these known patterns:\n\n${
        Object.entries(opts.reviewPlaybook)
          .map(([k, v]) => `### ${k}\n${v}`)
          .join('\n\n')
      }\n`
    : '';

  const truncationNotice = opts.changedFiles && opts.changedFiles.length > 0
    ? `\n\n⚠️ WARNING: The diff above was truncated. The following files were changed but may not be fully shown:\n${opts.changedFiles.map(f => `- ${f}`).join('\n')}\nRead these files directly with cat/head. List each file you read in the "truncated_files_read" field of your JSON output.`
    : '';
  const diffSection = opts.diff
    ? `\n## Diff (changes since base branch)\n\`\`\`diff\n${opts.diff}\n\`\`\`${truncationNotice}\n`
    : '\n## Diff\nNo diff available — review the changed files directly.\n';

  const buildConfig = opts.buildConfig;
  let buildVerificationSection: string;
  if (buildConfig && buildConfig.directories && buildConfig.directories.length > 0) {
    const checks = buildConfig.directories
      .filter(d => d.build_cmd || d.lint_cmd)
      .map(d => `- \`${d.build_cmd || d.lint_cmd}\` (in \`${d.path}/\`)`);
    if (checks.length > 0) {
      buildVerificationSection = `\n## Build Verification\nRun these build commands. If any fail, fix the errors before proceeding with the review checklist.\n\n${checks.join('\n')}\n`;
    } else {
      buildVerificationSection = '';
    }
  } else {
    buildVerificationSection = `\n## Build Verification\nRun these build commands. If any fail, fix the errors before proceeding with the review checklist.\n\n- For frontend changes (admin-ui/): \`cd admin-ui && npm run build\`\n- For agent changes (agent/): \`cd agent && npx tsc --noEmit\`\n- For backend changes (backend/): \`cd backend && python -m py_compile app/main.py\`\n`;
  }

  const scopeOverflowSection = opts.scopeOverflowWarning
    ? `\n## ⚠️ Scope Overflow Warning\n${opts.scopeOverflowWarning}\nFor each file listed, confirm it is necessary for this task. Revert any files that are not essential.\n`
    : '';

  const rc = opts.remediationContext;
  const remediationSection = rc && (rc.rootCause || rc.recommendedFix || rc.diagnosisTaskId)
    ? (() => {
        const lines = ['## Remediation Review'];
        lines.push('This PR was created to fix a diagnosed issue. Verify the changes specifically address the diagnosed root cause rather than doing a generic review.');
        if (rc.severity) lines.push(`- Severity: ${rc.severity}`);
        if (rc.confidence) lines.push(`- Diagnosis confidence: ${rc.confidence}`);
        if (rc.rootCause) lines.push(`- Root cause: ${rc.rootCause}`);
        if (rc.recommendedFix) lines.push(`- Recommended fix: ${rc.recommendedFix}`);
        return '\n' + lines.join('\n') + '\n';
      })()
    : '';

  const planSection = opts.analysisPlan && opts.analysisPlan.trim()
    ? `\n## Pre-Implementation Plan (verify changes align with this)\n${opts.analysisPlan.trim()}\n`
    : '';

  const pc = opts.planCompletion;
  const planCompletionSection = pc && (pc.plannedProductionCount > 0 || pc.plannedTestCount > 0)
    ? (() => {
        const missingLines = [
          ...pc.missingProduction.map(f => `- ${f} (production)`),
          ...pc.missingTest.map(f => `- ${f} (test)`),
        ];
        const missingBlock = missingLines.length > 0
          ? `\nMissing:\n${missingLines.join('\n')}`
          : '';
        return `\n## Plan Completion Status\n${pc.changedPlannedProductionCount}/${pc.plannedProductionCount} planned production files changed, ${pc.changedPlannedTestCount}/${pc.plannedTestCount} planned test files created or modified.${missingBlock}\n`;
      })()
    : '';

  const hasAnalysisPlan = !!(opts.analysisPlan && opts.analysisPlan.trim());
  const modelsChanged = !!opts.changedFiles?.some(f => f === 'backend/app/models.py' || f.endsWith('/backend/app/models.py'));
  const mandatoryChecks = getMandatoryChecks(opts.taskScope, hasAnalysisPlan, modelsChanged);
  const checkIdsNote = `Use these exact IDs in checks_performed: ${mandatoryChecks.join(', ')}`;

  return `You are performing a self-review of the changes you just made for this task.
${restrictionsSection}${remediationSection}${directionFeedbackSection}${scopeSpecificSection}${issueDistSection}${firstAttemptSection}${moduleFeedbackSection}${topIssuesSection}${playbookSection}${scopeOverflowSection}${planSection}${planCompletionSection}${diffSection}${buildVerificationSection}
## Self-Review Instructions

Review ONLY for CRITICAL and MAJOR issues. Skip minor style or naming feedback.

${buildSelfReviewChecklist(opts.taskScope, hasAnalysisPlan, modelsChanged)}

If you find CRITICAL or MAJOR issues, fix them and stage the changes with \`git add\`.

When done (whether or not you found issues), output a JSON block like one of these:

PASS example (all checks passed):
\`\`\`json
{"verdict":"PASS","check_results":{"test_coverage":{"passed":true},"type_safety":{"passed":true}},"checks_performed":["test_coverage","type_safety"],"issues_found":0,"files_reviewed":3,"truncated_files_read":[]}
\`\`\`

FAIL example (at least one check failed — verdict MUST be FAIL and issues_found MUST be ≥1):
\`\`\`json
{"verdict":"FAIL","check_results":{"test_coverage":{"passed":true},"type_safety":{"passed":false,"issue":"Missing return type on exportFoo"}},"checks_performed":["test_coverage","type_safety"],"issues_found":1,"files_reviewed":3,"truncated_files_read":[]}
\`\`\`

Fields:
- \`verdict\`: "PASS" if every check has passed:true, "FAIL" if any check has passed:false — these MUST be consistent with check_results
- \`check_results\`: object mapping each mandatory check ID to \`{passed: boolean, issue?: string}\` — ${checkIdsNote}. Set \`issue\` to a short description of what was found (omit if passed).
- \`checks_performed\`: IDs of every checklist item you examined (same keys as check_results, kept for backwards compatibility)
- \`issues_found\`: number of issues found (0 if none)
- \`files_reviewed\`: number of files you read or examined
- \`truncated_files_read\`: paths of files you read directly to compensate for a truncated diff (empty array if diff was not truncated)`;
}

// Format a blind-spots section describing what the create task's self-review
// verified (via truncated_files_read) versus which changed files it did not
// re-read. When self-review was flagged incomplete_review (diff truncated and
// the retry still reported zero files read), the reviewer needs to scrutinize
// unverified files — the self-review's PASS verdict did not examine them.
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
  const incomplete = categories.includes('incomplete_review');
  if (!incomplete && truncatedFilesRead.length === 0) return '';

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
  return lines.join('\n') + '\n\n';
}
