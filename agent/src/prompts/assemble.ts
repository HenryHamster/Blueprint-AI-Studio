/**
 * Shared prompt assembly for Anthropic prefix caching: concatenate a stable
 * leading prefix and a volatile trailing body, returning the split so callers
 * (and tests) can measure and assert the cache boundary.
 *
 * Anthropic's automatic prefix cache matches from byte 0 forward: the cache
 * prefix extends only up to the first byte that differs from the previous call,
 * so a variable byte appearing early busts the match for everything after it.
 * A byte-identical `stablePrefix` across calls makes the cacheable region as
 * long as possible; the `variableBody` is always re-read at full price.
 *
 * This is the shared concatenation used by prompt builders that follow the
 * stable-first / volatile-last structure (see `assembleStandardPrompt` in
 * `agent/src/handlers/standard.ts` and the strategic thinker split in
 * `agent/src/prompts/thinker.ts`).
 */
export interface AssembledStableVariablePrompt {
  /** Leading, cacheable region — must be byte-identical across calls that share it. */
  stablePrefix: string;
  /** Trailing, per-call region — always re-read at full price. */
  variableBody: string;
  /** The assembled prompt actually sent to the model. */
  finalPrompt: string;
}

export function assembleStableVariablePrompt(
  stablePrefix: string,
  variableBody: string,
): AssembledStableVariablePrompt {
  return { stablePrefix, variableBody, finalPrompt: stablePrefix + variableBody };
}
