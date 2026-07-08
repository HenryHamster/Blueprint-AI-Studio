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
 * expose cached tokens only as a sub-count.
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
  const promptDetails = objectValue(usage.prompt_tokens_details);
  const inputDetails = objectValue(usage.input_tokens_details);
  const detailedCachedTokens = Math.max(
    nonNegativeNumber(promptDetails?.cached_tokens),
    nonNegativeNumber(inputDetails?.cached_tokens),
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
