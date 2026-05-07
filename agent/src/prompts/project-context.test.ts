import { describe, it, expect } from 'vitest';
import { formatProjectContext } from './project-context.js';

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

  it('caps the FP calibration block to top-5 categories by FP rate', () => {
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
