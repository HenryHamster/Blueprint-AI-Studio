import type { BuildConfig } from '../index.js';
import { getMandatoryChecks } from './analysis.js';

function buildSelfReviewChecklist(taskScope?: string, hasAnalysisPlan?: boolean): string {
  if (taskScope === 'small') {
    return `Severity guide: CRITICAL = data loss / security breach / crash; MAJOR = incorrect behavior / missing coverage; MINOR = style / naming (skip).

Check for:
1. **Correctness** (MAJOR/CRITICAL) — Off-by-one errors, null/undefined handling, edge cases in the changed logic?
2. **Missing test coverage** (MAJOR) — Is there new behavior with no corresponding test?
3. **Error handling** (MAJOR) — Are async operations properly awaited, exceptions caught where needed, null checks present?
4. **Security quick-check** (CRITICAL) — Are any user inputs passed unsanitized to queries, commands, or HTML output?
5. **Import and type verification** (MAJOR) — Are new imports available, type annotations correct, and do modified exports match their callers?`;
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
9. **Import/dependency verification** — Are new imports available and correctly referenced?`;
  }
  // medium or unspecified: 8-item checklist (includes cross-file and import checks for multi-file changes)
  const planAlignmentItem = hasAnalysisPlan
    ? `0. **Plan alignment (HARD GATE — verdict MUST be FAIL if violated)** — Bidirectional check: (a) Do the files you actually changed match the "Files to change" list from the analysis plan? Test files corresponding to planned production files count as in-plan; any other unplanned non-test file is a scope violation. (b) Were all planned production files actually changed, and were files listed under "Tests to add or update" actually created or modified? See "Plan Completion Status" above — for each missing file, complete the planned change. Were any interfaces marked "preserve" altered? If any condition is violated, the verdict MUST be FAIL — revert unplanned changes and complete missing planned files. Justification is NOT acceptable.\n`
    : '';
  const scopeViolationsItem = hasAnalysisPlan
    ? `3. **Scope violations (HARD GATE — verdict MUST be FAIL if violated)** — Any changed non-test file not listed in the analysis plan's "Files to change" section is a scope violation and MUST result in a FAIL verdict. Test files for planned production files are in-scope. Revert unplanned non-test files; do not justify them.`
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
8. **Import/dependency verification** — Are new imports available and correctly referenced?`;
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
  persistentReviewIssues?: string[];
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
  scopeSpecificReviewIssues?: Array<{ description: string; count: number; category: string; scope: string }>;
}): string {
  const restrictionsSection = opts.claudeMd
    ? `\n## File Restrictions (from CLAUDE.md)\n${opts.claudeMd.slice(0, 500)}\n`
    : '';

  const reviewIssueDist = opts.reviewIssueDistribution;
  const issueDistSection = reviewIssueDist && Object.keys(reviewIssueDist).length > 0
    ? (() => {
        const sorted = Object.entries(reviewIssueDist)
          .sort(([, a], [, b]) => b - a)
          .slice(0, 5)
          .map(([cat, count]) => `${cat} (${Math.round(count * 100)}%)`)
          .join(', ');
        return `\n## Project-Specific Review Patterns\nIn this project, the most common review issues are: ${sorted}. Prioritize these checks.\n`;
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
  const mandatoryChecks = getMandatoryChecks(opts.taskScope, hasAnalysisPlan);
  const checkIdsNote = `Use these exact IDs in checks_performed: ${mandatoryChecks.join(', ')}`;

  return `You are performing a self-review of the changes you just made for this task.
${restrictionsSection}${scopeSpecificSection}${issueDistSection}${firstAttemptSection}${topIssuesSection}${playbookSection}${scopeOverflowSection}${planSection}${planCompletionSection}${diffSection}${buildVerificationSection}
## Self-Review Instructions

Review ONLY for CRITICAL and MAJOR issues. Skip minor style or naming feedback.

${buildSelfReviewChecklist(opts.taskScope, hasAnalysisPlan)}

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
