// ── Pre-implementation analysis prompt builder ───────────────────────────────
/**
 * Builds a read-only analysis prompt for medium-scope tasks.
 * The agent reads related files, maps module interfaces and dependencies,
 * then outputs a compact implementation plan before any code is written.
 */
export function buildAnalysisPrompt(opts: {
  rawPrompt: string;
  taskTitle: string;
  relatedFiles: string[];
  fileTree: string;
  narrowingGuidance?: string;
  fileLimit?: number;
  totalFileBudget?: number;
  directionConcurrentFiles?: string[];
  cochangeFiles?: string[];
  recentChanges?: {
    deployedAt?: string;
    mergedPrCount?: number;
    commits?: string[];
  };
  diagnosisContext?: {
    rootCause?: string;
    recommendedFix?: string;
    priorFailedApproaches?: string[];
  };
}): string {
  const diag = opts.diagnosisContext;
  let diagnosisBlock = '';
  if (diag && (diag.rootCause || diag.recommendedFix || (diag.priorFailedApproaches && diag.priorFailedApproaches.length > 0))) {
    const parts: string[] = [
      '\n\n## Diagnosis Context',
      'This task is a **diagnosed remediation**, not a new feature. A prior investigation has already identified the root cause and recommended a fix. Focus your analysis narrowly on the files and call sites implicated by the diagnosis — do NOT broadly map module interfaces unrelated to the diagnosed issue.',
    ];
    if (diag.rootCause) parts.push(`- **Root cause:** ${diag.rootCause.slice(0, 1200)}`);
    if (diag.recommendedFix) parts.push(`- **Recommended fix:** ${diag.recommendedFix.slice(0, 1200)}`);
    if (diag.priorFailedApproaches && diag.priorFailedApproaches.length > 0) {
      parts.push('- **Prior failed remediation approaches (avoid repeating):**');
      for (const a of diag.priorFailedApproaches.slice(0, 5)) {
        parts.push(`  - ${a.slice(0, 500)}`);
      }
    }
    parts.push('Scope your **Files to change**, **Files to verify (no changes expected)**, and **Callers to verify** to the diagnosed root cause. If the recommended fix would not work, note the concrete reason in **Implementation approach** before proposing an alternative that addresses the same root cause.');
    diagnosisBlock = parts.join('\n');
  }
  const filesList = opts.relatedFiles.length > 0
    ? `## Files Relevant to This Task\n${opts.relatedFiles.map(f => `- ${f}`).join('\n')}\n\n`
    : '';
  const concurrentBlock = opts.directionConcurrentFiles && opts.directionConcurrentFiles.length > 0
    ? `\n## Files currently being modified by sibling tasks in this direction\nThese files are in-flight on parallel tasks sharing this research direction. Avoid planning changes that overlap with them to prevent merge conflicts and duplicated work. If you must touch one, note the coordination risk in **Implementation approach**.\n${opts.directionConcurrentFiles.map(f => `- ${f}`).join('\n')}\n`
    : '';
  const cochangeBlock = opts.cochangeFiles && opts.cochangeFiles.length > 0
    ? `\n## Historically co-changed files (consider including in your plan if relevant to this change)\nThese files have frequently been modified together with this task's related files in past merged PRs. Use this as a hint when deciding which files belong in your **Files to change** list — include ones that genuinely need to change for this task, and deprioritize the rest so you stay within the file budget.\n${opts.cochangeFiles.map(f => `- ${f}`).join('\n')}\n`
    : '';
  const recentChangesBlock = opts.recentChanges && (opts.recentChanges.deployedAt || opts.recentChanges.mergedPrCount !== undefined || (opts.recentChanges.commits && opts.recentChanges.commits.length > 0))
    ? `\n## Recent Changes\n${opts.recentChanges.deployedAt ? `- Most recent deploy: ${opts.recentChanges.deployedAt}` : '- Most recent deploy: unknown'}${typeof opts.recentChanges.mergedPrCount === 'number' ? `\n- Merged PRs in window: ${opts.recentChanges.mergedPrCount}` : ''}${opts.recentChanges.commits && opts.recentChanges.commits.length > 0 ? `\n- Commits in window:\n${opts.recentChanges.commits.slice(0, 30).map(c => `  - ${c.slice(0, 180)}`).join('\n')}` : ''}\n`
    : '';
  const titleLine = opts.taskTitle ? `**Title:** ${opts.taskTitle}\n\n` : '';
  const limitClause = typeof opts.fileLimit === 'number'
    ? ` Aim for ${opts.fileLimit} or fewer files; if you cannot stay at or below ${opts.fileLimit}, keep the files that must change together for the change to be complete and correct (never drop a caller/consumer of a contract you are changing), and note in **Implementation approach** what independent concern was deferred.`
    : '';
  const totalBudgetClause = typeof opts.totalFileBudget === 'number'
    ? `\n\n**Total file budget:** the count of production files under **Files to change** must not exceed ${opts.totalFileBudget}. Test files under **Tests to add or update** do NOT count toward this budget — plan whatever test coverage the change requires. Plans exceeding the production-file budget will be rejected and you will be asked to narrow scope.`
    : '';
  const narrowingBlock = opts.narrowingGuidance
    ? `\n\n## Narrowing Directive (from prior analysis pass)\n${opts.narrowingGuidance}\n`
    : '';

  return `${opts.fileTree}${filesList}You are performing a pre-implementation analysis for a medium-scope task. This is a READ-ONLY pass — do not modify any files.

## Task
${titleLine}${opts.rawPrompt.slice(0, 1500)}${recentChangesBlock}${diagnosisBlock}${narrowingBlock}${concurrentBlock}${cochangeBlock}

## Instructions

1. Read the files listed in "Files Relevant to This Task" above. If no files are listed, identify the relevant files from the file tree and read them.

2. For each relevant file, identify:
   - What it exports (functions, classes, types, constants)
   - What it imports from other local files
   - Key interfaces or type contracts it defines or depends on

3. Map the dependencies between files: which files import from which others. For each module you plan to change, search the codebase for production files (non-test) that import from it and note the specific symbols they consume — these are the callers you will need to verify after implementation.

4. Output a compact structured implementation plan using exactly these headings:

**Files to change** (list in recommended change order, one per line with brief reason. Only include files you actually plan to MODIFY. Files you need to READ to confirm compatibility but do not plan to modify belong under **Files to verify (no changes expected)** below — do not inflate this list with compatibility-only checks).${limitClause}

**Files to verify (no changes expected)** (list files you need to READ to confirm correctness — e.g. consumers that import a changed type, callers whose call-site you must confirm still compiles — but do NOT plan to modify. One per line with the reason you need to verify it. These files do NOT count toward the production-file budget. Write "none" with a one-line justification if no verification-only reads are needed.)

**Interfaces to preserve** (function signatures, exported types, or API contracts that must not be broken)

**Key dependencies** (which modules call into which — determines safe change order)

**Implementation approach** (2-3 sentences describing the strategy to make changes correctly without breaking type contracts)

**Callers to verify after implementation** (for each module being changed, list the production files that import it and the specific symbols they use — format: \`consumer/file.ts (uses: symbolA, symbolB)\`. Write "none found" with a one-line justification if no external callers exist. This is the concrete post-implementation verification checklist.)

**Tests to add or update** (list each test file you will create or modify, one per line, with the case name or behavior covered)

**Tests at risk of breaking** (list existing tests whose assertions or fixtures may need adjustment because of the planned change — name the file and the specific test or describe "none expected" with a one-line justification)${totalBudgetClause}

Keep the total output under 700 words. Be specific and concrete — name actual function signatures, type names, and import paths. Do NOT write any implementation code.`;
}

/**
 * Extracts file path tokens from bullet/numbered list items appearing under
 * the first occurrence of any heading in `headingRes`. Stops at the next bold
 * heading. Used by both production-file and test-file parsers below.
 */
function collectFilesUnderHeadings(analysisOutput: string, headingRes: RegExp[]): string[] {
  if (!analysisOutput) return [];
  const lines = analysisOutput.split('\n');
  const nextHeadingRe = /^\s*\*\*[^*]+\*\*/;
  const markerRe = /^\s*(?:[-*•]|\d+[.)])\s+/;

  const out: string[] = [];
  const seen = new Set<string>();

  for (const headingRe of headingRes) {
    let i = 0;
    for (; i < lines.length; i++) {
      if (headingRe.test(lines[i])) break;
    }
    if (i >= lines.length) continue;
    i++;

    for (; i < lines.length; i++) {
      const raw = lines[i];
      if (raw.trim() === '') continue;
      if (nextHeadingRe.test(raw)) break;
      if (!markerRe.test(raw)) continue;
      const stripped = raw.replace(markerRe, '');
      let token = stripped.trim().split(/\s+/)[0] || '';
      token = token.replace(/^[`"'(]+|[`"',.;:)]+$/g, '');
      if (!token || !/[/.]/.test(token)) continue;
      if (seen.has(token)) continue;
      seen.add(token);
      out.push(token);
    }
  }
  return out;
}

/**
 * Parses the **Files to change** section of an analysis prompt's output and
 * returns the list of file paths the agent intends to modify. The parser is
 * intentionally permissive: it accepts bullet (`-`, `*`, `•`) and numbered
 * (`1.`, `1)`) list markers, with or without backticks, and stops at the next
 * bold heading.
 *
 * Returns an empty array when the section is missing or unparsable.
 */
export function parseAnalysisPlanFiles(analysisOutput: string): string[] {
  return collectFilesUnderHeadings(analysisOutput, [/^\s*\*\*Files to change\*\*/i]);
}

/**
 * Parses the **Files to verify (no changes expected)** section of an analysis
 * prompt's output and returns the list of file paths the agent needs to READ
 * to confirm correctness but does NOT plan to modify. These files are
 * deliberately EXCLUDED from the analysis-plan production-file budget — the
 * parser exists so the verify list can be surfaced in logs and threaded into
 * the post-implementation self-review context.
 *
 * Returns an empty array when the section is missing or unparsable.
 */
export function parseAnalysisPlanVerifyFiles(analysisOutput: string): string[] {
  return collectFilesUnderHeadings(analysisOutput, [/^\s*\*\*Files to verify\b/i]);
}

/**
 * Parses the **Tests to add or update** and **Tests at risk of breaking**
 * sections and returns the deduped test file paths. Same parser semantics as
 * parseAnalysisPlanFiles. Test files are deliberately EXCLUDED from the
 * analysis-plan production-file budget so coverage can scale with the
 * production change; this parser exists purely to surface the planned test
 * files in log output and in the narrowing-retry guidance (so the LLM can
 * see what coverage was already planned when it narrows scope).
 */
export function parseAnalysisPlanTestFiles(analysisOutput: string): string[] {
  return collectFilesUnderHeadings(analysisOutput, [
    /^\s*\*\*Tests to add or update\*\*/i,
    /^\s*\*\*Tests at risk of breaking\*\*/i,
  ]);
}
