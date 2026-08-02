/** Small helpers for the consolidated author self-review path. */

export const VIEWPORT_INFRA_SKIP_GUIDANCE = `## Viewport gate: infra-skip sentinel

If \`./check\` output contains \`VIEWPORT_GATE_INFRA_UNAVAILABLE:\` OR the
"admin-ui playwright chromium" bootstrap step fails AND \`docker info\` also
errors (Docker unavailable), this is an infrastructure shortfall — NOT a code
defect. Chromium and its system-library dependencies cannot be installed by a
code edit on this VM. Call \`./blocked "VIEWPORT_GATE_INFRA_UNAVAILABLE: <reason>"\`
immediately. Do NOT attempt to install Chromium, modify viewport tests to bypass
the browser requirement, or weaken test assertions to work around the missing runtime.
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
