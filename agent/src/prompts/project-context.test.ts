import { describe, it, expect } from 'vitest';
import { formatProjectContext, formatSrCatchRateForSrPrompt } from './project-context.js';

describe('formatProjectContext — Self-Review False-Positive Calibration', () => {
  it('renders the FP calibration block when a category exceeds the FP threshold and has adequate sample size', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.05,
            false_positive_rate: 0.8,
            precision: 0.2,
            sr_flag_sample_size: 15,
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### Self-Review False-Positive Calibration');
    expect(result).toContain('security');
    expect(result).toContain('80% false-positive');
    // Verbose phrasing must explicitly tell the agent NOT to weaken genuine catches
    expect(result).toContain('do not weaken genuine catches');
  });

  it('omits the FP calibration block when no category meets the threshold (low sample size)', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.05,
            false_positive_rate: 0.9,
            sr_flag_sample_size: 5, // below 10
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review False-Positive Calibration');
  });

  it('omits the FP calibration block when FP rate is below threshold', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.10,
            false_positive_rate: 0.3, // below 0.5
            sr_flag_sample_size: 20,
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review False-Positive Calibration');
  });

  it('omits the FP calibration block when self_review_catch_rate is absent', () => {
    const ctx = {};
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Self-Review False-Positive Calibration');
  });

  it('renders blind-spots and FP-calibration blocks side-by-side when both apply (regression check)', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.05,
            false_positive_rate: 0.8,
            sr_flag_sample_size: 15,
          },
          'error-handling': {
            miss_rate: 0.6,
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### Self-Review Blind Spots');
    expect(result).toContain('### Self-Review False-Positive Calibration');
    // Calibration block must come AFTER blind-spots block
    const blindIdx = result.indexOf('### Self-Review Blind Spots');
    const fpIdx = result.indexOf('### Self-Review False-Positive Calibration');
    expect(blindIdx).toBeLessThan(fpIdx);
  });

  it('prefers for_task_type over categories and tags scope note', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          security: {
            miss_rate: 0.10,
            false_positive_rate: 0.6,
            sr_flag_sample_size: 12,
          },
        },
        for_task_type: {
          'type-safety': {
            miss_rate: 0.05,
            false_positive_rate: 0.85,
            sr_flag_sample_size: 20,
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### Self-Review False-Positive Calibration');
    expect(result).toContain('type-safety');
    expect(result).toContain('(create tasks only)');
    // The category-level entry must NOT appear when for_task_type is preferred.
    expect(result).not.toContain('**security**: flagged');
  });

  it('instructs agents to add reasoning for regressing blind-spot categories', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          correctness: { miss_rate: 0.4 },
          'test-coverage': { miss_rate: 0.3 },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### Self-Review Blind Spots');
    // Must tell the agent to add a reasoning field for these categories
    expect(result).toContain('reasoning');
    expect(result).toContain('check_results');
    // Instruction must cover both pass and fail outcomes
    expect(result).toContain('pass and fail');
  });

  it('caps the FP calibration block to top-5 categories by FP rate (regression check)', () => {
    const cats: Record<string, { miss_rate: number; false_positive_rate: number; sr_flag_sample_size: number }> = {};
    for (let i = 0; i < 8; i++) {
      cats[`cat-${i}`] = {
        miss_rate: 0.05,
        false_positive_rate: 0.5 + i * 0.05, // 0.50..0.85
        sr_flag_sample_size: 12,
      };
    }
    const ctx = { self_review_catch_rate: { categories: cats } };
    const result = formatProjectContext(ctx, 'create');
    // Highest FP rate categories (cat-7..cat-3) should appear; the lowest (cat-0, cat-1, cat-2) excluded.
    expect(result).toContain('cat-7');
    expect(result).toContain('cat-3');
    expect(result).not.toContain('**cat-0**');
    expect(result).not.toContain('**cat-1**');
    expect(result).not.toContain('**cat-2**');
  });
});

describe('formatSrCatchRateForSrPrompt', () => {
  it('renders miss-rate categories with percentages', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          correctness: { miss_rate: 0.35 },
          security: { miss_rate: 0.20 },
        },
      },
    };
    const result = formatSrCatchRateForSrPrompt(ctx);
    expect(result).toContain('Categories you historically miss');
    expect(result).toContain('correctness: miss rate 35%');
    expect(result).toContain('security: miss rate 20%');
    expect(result).toContain('extra skepticism');
  });

  it('includes trend delta when previous and current hit rates are present', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          'error-handling': {
            miss_rate: 0.40,
            previous_hit_rate: 0.75, // prev miss 25%
            current_hit_rate: 0.60,  // curr miss 40% → +15pp
          },
        },
      },
    };
    const result = formatSrCatchRateForSrPrompt(ctx);
    expect(result).toContain('+15pp vs last cycle');
  });

  it('uses for_task_type when present and tags scope note', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: { correctness: { miss_rate: 0.10 } },
        for_task_type: { 'type-safety': { miss_rate: 0.45 } },
      },
    };
    const result = formatSrCatchRateForSrPrompt(ctx, 'create');
    expect(result).toContain('type-safety');
    expect(result).toContain('(create tasks)');
    expect(result).not.toContain('correctness');
  });

  it('returns empty string when no catch-rate data is present', () => {
    expect(formatSrCatchRateForSrPrompt({})).toBe('');
    expect(formatSrCatchRateForSrPrompt({ self_review_catch_rate: null })).toBe('');
  });
});
