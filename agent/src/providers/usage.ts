export interface ProviderTokenUsage {
  inputTokens: number;
  outputTokens: number;
  cacheCreationTokens: number;
  cacheReadTokens: number;
}

function nonNegativeNumber(value: unknown): number {
  const n = Number(value ?? 0);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null;
}

/**
 * Normalize provider usage blocks into Blueprint's inclusive-input contract.
 *
 * Anthropic-compatible streams report `input_tokens` excluding explicit cache
 * classes, so inclusive input is input + cache_creation + cache_read. OpenAI-
 * style streams report prompt/input tokens as an already-inclusive total and
 * expose cached tokens only as a sub-count (prompt_tokens_details.cached_tokens,
 * input_tokens_details.cached_tokens, or the Codex CLI's top-level
 * `cached_input_tokens`).
 */
export function normalizeProviderTokenUsage(
  usage: Record<string, unknown>,
): ProviderTokenUsage | undefined {
  const inputTokens = nonNegativeNumber(usage.input_tokens);
  const outputTokens =
    nonNegativeNumber(usage.output_tokens) || nonNegativeNumber(usage.completion_tokens);
  const cacheCreationTokens = nonNegativeNumber(usage.cache_creation_input_tokens);
  const cacheReadTokens = nonNegativeNumber(usage.cache_read_input_tokens);

  const promptTokens = nonNegativeNumber(usage.prompt_tokens);
  const promptCacheHitTokens = nonNegativeNumber(usage.prompt_cache_hit_tokens);
  const promptCacheMissTokens = nonNegativeNumber(usage.prompt_cache_miss_tokens);
  // Codex CLI reports the cached-input sub-count as a top-level sibling inside
  // usage ({input_tokens, cached_input_tokens, output_tokens}) — NOT under
  // prompt_tokens_details / input_tokens_details. Without reading it, every
  // codex/OpenAI run's cache hits silently fall through to the Anthropic branch
  // and are priced as uncached input (the $207 gpt-5.6-sol at zero cache reads
  // defect). Fold it into the same cached-token source set.
  const codexCachedInputTokens = nonNegativeNumber(usage.cached_input_tokens);
  const promptDetails = objectValue(usage.prompt_tokens_details);
  const inputDetails = objectValue(usage.input_tokens_details);
  const detailedCachedTokens = Math.max(
    nonNegativeNumber(promptDetails?.cached_tokens),
    nonNegativeNumber(inputDetails?.cached_tokens),
    codexCachedInputTokens,
  );

  const hasOpenAiShape =
    promptTokens > 0
    || promptCacheHitTokens > 0
    || promptCacheMissTokens > 0
    || detailedCachedTokens > 0
    || usage.completion_tokens !== undefined;

  if (hasOpenAiShape) {
    const inclusiveInputTokens =
      promptTokens || inputTokens || promptCacheHitTokens + promptCacheMissTokens;
    const cachedTokens = Math.min(
      Math.max(promptCacheHitTokens, detailedCachedTokens),
      inclusiveInputTokens,
    );
    if (inclusiveInputTokens <= 0 && outputTokens <= 0) return undefined;
    return {
      inputTokens: inclusiveInputTokens,
      outputTokens,
      cacheCreationTokens: 0,
      cacheReadTokens: cachedTokens,
    };
  }

  const inclusiveInputTokens = inputTokens + cacheCreationTokens + cacheReadTokens;
  if (inclusiveInputTokens <= 0 && outputTokens <= 0) return undefined;
  return {
    inputTokens: inclusiveInputTokens,
    outputTokens,
    cacheCreationTokens,
    cacheReadTokens,
  };
}

/**
 * Extract RAW provider-reported usage into Blueprint's persisted four-field
 * shape ({input_tokens, output_tokens, cache_creation_input_tokens,
 * cache_read_input_tokens}).
 *
 * Unlike normalizeProviderTokenUsage (which returns the INCLUSIVE input total
 * for api_call_summary), this preserves the uncached/cache-creation/cache-read
 * split so the three input classes SUM to the provider's inclusive total and
 * the Component B reconciliation stays consistent:
 *   - Anthropic-style streams: input_tokens is already uncached, with the two
 *     cache classes as explicit siblings.
 *   - OpenAI/DeepSeek-style streams: prompt_tokens is the INCLUSIVE total and
 *     the cached portion is only a sub-count (prompt_cache_hit_tokens,
 *     prompt_tokens_details.cached_tokens, or the Codex CLI's top-level
 *     `cached_input_tokens`), so uncached = inclusive - cached.
 *
 * Returns undefined when the block carries no usable token fields (the caller
 * records the not-reported marker separately from the absence of a usage block).
 */
export function extractProviderReportedUsage(
  usage: Record<string, unknown>,
): ProviderTokenUsage | undefined {
  const inputTokens = nonNegativeNumber(usage.input_tokens);
  const outputTokens =
    nonNegativeNumber(usage.output_tokens) || nonNegativeNumber(usage.completion_tokens);
  const cacheCreationTokens = nonNegativeNumber(usage.cache_creation_input_tokens);
  const cacheReadTokens = nonNegativeNumber(usage.cache_read_input_tokens);

  const promptTokens = nonNegativeNumber(usage.prompt_tokens);
  const promptCacheHitTokens = nonNegativeNumber(usage.prompt_cache_hit_tokens);
  const promptCacheMissTokens = nonNegativeNumber(usage.prompt_cache_miss_tokens);
  // Codex CLI cached-input sub-count (see normalizeProviderTokenUsage).
  const codexCachedInputTokens = nonNegativeNumber(usage.cached_input_tokens);
  const promptDetails = objectValue(usage.prompt_tokens_details);
  const inputDetails = objectValue(usage.input_tokens_details);
  const detailedCachedTokens = Math.max(
    nonNegativeNumber(promptDetails?.cached_tokens),
    nonNegativeNumber(inputDetails?.cached_tokens),
    codexCachedInputTokens,
  );

  const hasOpenAiShape =
    promptTokens > 0
    || promptCacheHitTokens > 0
    || promptCacheMissTokens > 0
    || detailedCachedTokens > 0
    || usage.completion_tokens !== undefined;

  if (hasOpenAiShape) {
    const inclusiveInputTokens =
      promptTokens || inputTokens || promptCacheHitTokens + promptCacheMissTokens;
    const cachedTokens = Math.min(
      Math.max(promptCacheHitTokens, detailedCachedTokens),
      inclusiveInputTokens,
    );
    if (inclusiveInputTokens <= 0 && outputTokens <= 0) return undefined;
    return {
      inputTokens: Math.max(inclusiveInputTokens - cachedTokens, 0),
      outputTokens,
      cacheCreationTokens: 0,
      cacheReadTokens: cachedTokens,
    };
  }

  const totalInputTokens = inputTokens + cacheCreationTokens + cacheReadTokens;
  if (totalInputTokens <= 0 && outputTokens <= 0) return undefined;
  return {
    inputTokens,
    outputTokens,
    cacheCreationTokens,
    cacheReadTokens,
  };
}
