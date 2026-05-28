// Issue-string deduplication helpers extracted from review-fix.ts.
// These are pure functions with no I/O dependencies.

export type ReviewHistoryIssue = { severity: string; description: string; file?: string; line?: number };
export type ReviewHistoryEntry = {
  iteration: number;
  feedback: string;
  issues?: ReviewHistoryIssue[];
  verdict?: 'APPROVE' | 'REQUEST_CHANGES';
  changed_files?: string[];
};
export type AnnotatedIssue = ReviewHistoryIssue & { resolvedInIteration?: number };
export type AnnotatedEntry = { iteration: number; feedback: string; issues?: AnnotatedIssue[] };

export function fmtIssueLoc(i: { file?: string; line?: number }): string {
  if (i.file && typeof i.line === 'number') return ` [${i.file}:${i.line}]`;
  if (i.file) return ` [${i.file}]`;
  return '';
}

// Jaccard token-overlap threshold for treating a rephrased issue as the SAME
// concept. Reviewers often paraphrase between iterations ("missing null check"
// → "null check not present"); exact-match alone marks those as resolved and
// the next reviewer skips a real bug. False positives here are cheap (reviewer
// re-verifies an unrelated issue once); false negatives are expensive
// (persistent bug ships), so the threshold leans permissive.
export const ISSUE_SIMILARITY_THRESHOLD = 0.4;

const ISSUE_TOKEN_STOPWORDS = new Set([
  'the', 'a', 'an', 'is', 'in', 'on', 'at', 'of', 'to', 'for', 'and', 'or',
  'this', 'that', 'it', 'as', 'be', 'are', 'was', 'were', 'has', 'have', 'had',
  'with', 'by', 'from', 'into', 'when', 'if',
]);

function tokenizeIssue(s: string): Set<string> {
  return new Set(
    s.toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter(t => t.length >= 2 && !ISSUE_TOKEN_STOPWORDS.has(t))
  );
}

function jaccardSimilarity(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 || b.size === 0) return 0;
  let intersection = 0;
  for (const t of a) if (b.has(t)) intersection++;
  const union = a.size + b.size - intersection;
  return union === 0 ? 0 : intersection / union;
}

// Annotate each iteration's issues with whether they reappeared in the next
// iteration. Issues that don't reappear are likely resolved — surfacing that
// signal prevents the next reviewer from re-flagging already-fixed problems or
// wasting effort re-verifying them. Matches both exact descriptions (after
// whitespace/case normalization) and rephrasings (token overlap above
// ISSUE_SIMILARITY_THRESHOLD), so a paraphrased-but-persisting issue is not
// silently marked resolved. The most recent iteration is never annotated (no
// "next" to compare against).
export function annotateResolvedIssues(history: ReviewHistoryEntry[]): AnnotatedEntry[] {
  const sorted = [...history].sort((a, b) => a.iteration - b.iteration);
  const norm = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();
  return sorted.map((entry, idx) => {
    const next = sorted.at(idx + 1);
    if (!next || !entry.issues || entry.issues.length === 0) return entry;
    const nextIssues = next.issues || [];
    const nextDescs = new Set(nextIssues.map(i => norm(i.description)));
    const nextTokenSets = nextIssues.map(i => tokenizeIssue(i.description));
    const annotated: AnnotatedIssue[] = entry.issues.map(i => {
      if (nextDescs.has(norm(i.description))) return i;
      const currentTokens = tokenizeIssue(i.description);
      if (currentTokens.size > 0) {
        for (const nextTokens of nextTokenSets) {
          if (jaccardSimilarity(currentTokens, nextTokens) >= ISSUE_SIMILARITY_THRESHOLD) {
            return i;
          }
        }
      }
      return { ...i, resolvedInIteration: next.iteration };
    });
    return { ...entry, issues: annotated };
  });
}

export function fmtAnnotatedIssue(label: string, i: AnnotatedIssue): string {
  const base = `  [${label}]${fmtIssueLoc(i)} ${i.description}`;
  return i.resolvedInIteration
    ? `${base} (likely resolved — not flagged in iteration ${i.resolvedInIteration})`
    : base;
}
