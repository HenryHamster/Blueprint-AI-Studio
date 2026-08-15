/** Small helpers for the consolidated author self-review path. */

export const VIEWPORT_INFRA_SKIP_GUIDANCE = `## Viewport gate: CI-owned browser verification

When the VM cannot provide the Chromium substrate, \`./check\` and the viewport
capture helpers report \`VIEWPORT_CAPTURE_SKIPPED:\` with the concrete reason.
Record that skip and continue to \`./pr\`: this is an infrastructure shortfall,
not a code defect, and it must never be reported as a \`BLOCKER:\` or routed to
\`./blocked\`. Do not modify viewport tests or weaken assertions to work around
the missing runtime. The CI \`frontend-viewport\` job owns the browser-backed
viewport gate and must still fail on real viewport assertion failures.
`;

type SelfReviewCatchRateCategory = {
  miss_rate?: number;
  weighted_miss_rate?: number;
  engagement_thinning?: boolean;
};

function catchRateCategories(
  projectContext: Record<string, unknown> | undefined,
): Record<string, SelfReviewCatchRateCategory> | undefined {
  const catchRate = projectContext?.self_review_catch_rate as {
    categories?: Record<string, SelfReviewCatchRateCategory>;
  } | null | undefined;
  return catchRate?.categories;
}

export function getRegressingCategories(
  projectContext: Record<string, unknown> | undefined,
): string[] {
  const categories = catchRateCategories(projectContext);
  if (!categories) return [];
  return Object.entries(categories)
    .filter(([, data]) => typeof data.miss_rate === 'number' && data.miss_rate > 0)
    .sort((a, b) => (b[1].weighted_miss_rate ?? b[1].miss_rate ?? 0) - (a[1].weighted_miss_rate ?? a[1].miss_rate ?? 0))
    .map(([category]) => category);
}

export function getThinningCategories(
  projectContext: Record<string, unknown> | undefined,
): string[] {
  const categories = catchRateCategories(projectContext);
  if (!categories) return [];
  return Object.entries(categories)
    .filter(([, data]) => data.engagement_thinning === true)
    .map(([category]) => category);
}

/** Extract the structured author self-review object from an agent response. */
export function extractSelfReviewJson(output: string): Record<string, unknown> | null {
  const fenceMatch = output.match(/```json\s*\n?([\s\S]*?)\n?```/);
  if (fenceMatch) {
    try {
      return JSON.parse(fenceMatch[1].trim()) as Record<string, unknown>;
    } catch {
      // Fall through to a bare-object scan.
    }
  }
  const start = output.indexOf('{');
  const end = output.lastIndexOf('}');
  if (start === -1 || end <= start) return null;
  try {
    return JSON.parse(output.slice(start, end + 1)) as Record<string, unknown>;
  } catch {
    return null;
  }
}
