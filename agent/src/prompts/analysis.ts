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
}): string {
  const filesList = opts.relatedFiles.length > 0
    ? `## Files Relevant to This Task\n${opts.relatedFiles.map(f => `- ${f}`).join('\n')}\n\n`
    : '';
  const titleLine = opts.taskTitle ? `**Title:** ${opts.taskTitle}\n\n` : '';
  const limitClause = typeof opts.fileLimit === 'number'
    ? ` Aim for ${opts.fileLimit} or fewer files; if you cannot stay at or below ${opts.fileLimit}, list only the smallest viable set and note in **Implementation approach** what was deferred.`
    : '';
  const totalBudgetClause = typeof opts.totalFileBudget === 'number'
    ? `\n\n**Total file budget:** the combined count of files under **Files to change** PLUS test files under **Tests to add or update** must not exceed ${opts.totalFileBudget}. Plans that exceed this budget will be rejected and you will be asked to narrow scope. Count test files when planning.`
    : '';
  const narrowingBlock = opts.narrowingGuidance
    ? `\n\n## Narrowing Directive (from prior analysis pass)\n${opts.narrowingGuidance}\n`
    : '';

  return `${opts.fileTree}${filesList}You are performing a pre-implementation analysis for a medium-scope task. This is a READ-ONLY pass — do not modify any files.

## Task
${titleLine}${opts.rawPrompt.slice(0, 1500)}${narrowingBlock}

## Instructions

1. Read the files listed in "Files Relevant to This Task" above. If no files are listed, identify the relevant files from the file tree and read them.

2. For each relevant file, identify:
   - What it exports (functions, classes, types, constants)
   - What it imports from other local files
   - Key interfaces or type contracts it defines or depends on

3. Map the dependencies between files: which files import from which others. For each module you plan to change, search the codebase for production files (non-test) that import from it and note the specific symbols they consume — these are the callers you will need to verify after implementation.

4. Output a compact structured implementation plan using exactly these headings:

**Files to change** (list in recommended change order, one per line with brief reason).${limitClause}

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
 * Parses the **Tests to add or update** and **Tests at risk of breaking**
 * sections and returns the deduped test file paths. Same parser semantics as
 * parseAnalysisPlanFiles. Used to count test files toward the analysis-plan
 * total-file budget so a plan with 6 production + 5 test files is caught
 * before implementation rather than failing as scope_overflow afterward.
 */
export function parseAnalysisPlanTestFiles(analysisOutput: string): string[] {
  return collectFilesUnderHeadings(analysisOutput, [
    /^\s*\*\*Tests to add or update\*\*/i,
    /^\s*\*\*Tests at risk of breaking\*\*/i,
  ]);
}

// ── Self-review helpers ───────────────────────────────────────────────────────

/**
 * Returns the mandatory check IDs for the given task scope.
 * These must appear in the `checks_performed` field of the structured self-review output.
 */
export function getMandatoryChecks(taskScope?: string, hasAnalysisPlan?: boolean, modelsChanged?: boolean): string[] {
  let checks: string[];
  if (taskScope === 'small') {
    checks = ['correctness', 'test_coverage', 'import_verification'];
  } else if (taskScope === 'large') {
    checks = ['test_coverage', 'type_safety', 'scope_violations', 'security', 'pattern_violations', 'cross_file_consistency', 'backwards_compatibility', 'data_model_safety', 'import_verification'];
  } else {
    checks = ['test_coverage', 'type_safety', 'scope_violations', 'security', 'pattern_violations', 'cross_file_consistency', 'backwards_compatibility', 'import_verification'];
    if (hasAnalysisPlan) checks.unshift('plan_alignment');
  }
  if (modelsChanged) checks.push('migration_completeness');
  return checks;
}
