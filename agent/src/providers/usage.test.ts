import { describe, expect, it } from 'vitest';
import {
  normalizeProviderTokenUsage,
  extractProviderReportedUsage,
} from './usage.js';

describe('normalizeProviderTokenUsage', () => {
  it('normalizes Anthropic-style cache classes into inclusive input tokens', () => {
    expect(normalizeProviderTokenUsage({
      input_tokens: 100,
      output_tokens: 40,
      cache_creation_input_tokens: 10,
      cache_read_input_tokens: 5,
    })).toEqual({
      inputTokens: 115,
      outputTokens: 40,
      cacheCreationTokens: 10,
      cacheReadTokens: 5,
    });
  });

  it('does not double-count OpenAI cached prompt details', () => {
    expect(normalizeProviderTokenUsage({
      prompt_tokens: 100,
      completion_tokens: 40,
      prompt_tokens_details: { cached_tokens: 25 },
    })).toEqual({
      inputTokens: 100,
      outputTokens: 40,
      cacheCreationTokens: 0,
      cacheReadTokens: 25,
    });
  });

  it('does not double-count OpenAI Responses cached input details', () => {
    expect(normalizeProviderTokenUsage({
      input_tokens: 120,
      output_tokens: 45,
      input_tokens_details: { cached_tokens: 35 },
    })).toEqual({
      inputTokens: 120,
      outputTokens: 45,
      cacheCreationTokens: 0,
      cacheReadTokens: 35,
    });
  });

  it('normalizes Z.AI/OpenAI-compatible cached prompt details as cache reads', () => {
    expect(normalizeProviderTokenUsage({
      prompt_tokens: 180,
      completion_tokens: 20,
      total_tokens: 200,
      prompt_tokens_details: { cached_tokens: 160 },
    })).toEqual({
      inputTokens: 180,
      outputTokens: 20,
      cacheCreationTokens: 0,
      cacheReadTokens: 160,
    });
  });

  it('normalizes DeepSeek cache hit and miss tokens into inclusive prompt total', () => {
    expect(normalizeProviderTokenUsage({
      completion_tokens: 40,
      prompt_cache_hit_tokens: 30,
      prompt_cache_miss_tokens: 70,
    })).toEqual({
      inputTokens: 100,
      outputTokens: 40,
      cacheCreationTokens: 0,
      cacheReadTokens: 30,
    });
  });

  it('returns undefined for all-zero usage', () => {
    expect(normalizeProviderTokenUsage({
      input_tokens: 0,
      output_tokens: 0,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      prompt_tokens_details: { cached_tokens: 0 },
      input_tokens_details: { cached_tokens: 0 },
      prompt_cache_hit_tokens: 0,
      prompt_cache_miss_tokens: 0,
    })).toBeUndefined();
  });

  it('normalizes Codex CLI cached_input_tokens as cache reads', () => {
    // Codex CLI emits usage as {input_tokens, cached_input_tokens, output_tokens}
    // where input_tokens is the INCLUSIVE total and cached_input_tokens the
    // sub-count served from prompt cache. Without handling this shape the cache
    // hits fall through to the Anthropic branch and are priced as uncached.
    expect(normalizeProviderTokenUsage({
      input_tokens: 24_763,
      cached_input_tokens: 24_448,
      output_tokens: 122,
    })).toEqual({
      inputTokens: 24_763,
      outputTokens: 122,
      cacheCreationTokens: 0,
      cacheReadTokens: 24_448,
    });
  });

  it('normalizes zero-codex-cache usage as fully uncached input', () => {
    expect(normalizeProviderTokenUsage({
      input_tokens: 500,
      cached_input_tokens: 0,
      output_tokens: 80,
    })).toEqual({
      inputTokens: 500,
      outputTokens: 80,
      cacheCreationTokens: 0,
      cacheReadTokens: 0,
    });
  });
});

describe('extractProviderReportedUsage', () => {
  it('preserves the uncached/cache-read split for Codex CLI usage', () => {
    expect(extractProviderReportedUsage({
      input_tokens: 24_763,
      cached_input_tokens: 24_448,
      output_tokens: 122,
    })).toEqual({
      inputTokens: 315,
      outputTokens: 122,
      cacheCreationTokens: 0,
      cacheReadTokens: 24_448,
    });
  });

  it('preserves prompt_tokens_details.cached_tokens into cache_read_input_tokens', () => {
    expect(extractProviderReportedUsage({
      prompt_tokens: 100,
      completion_tokens: 40,
      prompt_tokens_details: { cached_tokens: 25 },
    })).toEqual({
      inputTokens: 75,
      outputTokens: 40,
      cacheCreationTokens: 0,
      cacheReadTokens: 25,
    });
  });
});
