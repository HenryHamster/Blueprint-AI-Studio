import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { formatProjectContext, formatSrCatchRateForSrPrompt } from './project-context.js';

const __dirname = dirname(fileURLToPath(import.meta.url));

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

describe('formatProjectContext — SR Recent Miss Examples', () => {
  it('renders section from sr_recent_miss_examples (backend contract)', () => {
    // Shape mirrors what backend/app/routers/agent.py emits as
    // response["project_context"]["sr_recent_miss_examples"].
    const ctx = {
      sr_recent_miss_examples: {
        security: [{ severity: 'CRITICAL', desc: 'SQL injection via user input' }],
        correctness: [{ severity: 'HIGH', desc: 'missed null check' }],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('## Concrete misses from the last cycle');
    expect(result).toContain('**security:**');
    expect(result).toContain('[CRITICAL] SQL injection via user input');
    expect(result).toContain('**correctness:**');
    expect(result).toContain('[HIGH] missed null check');
  });

  it('omits the section when sr_recent_miss_examples is absent or has no examples', () => {
    expect(formatProjectContext({}, 'create')).not.toContain('Concrete misses from the last cycle');
    expect(formatProjectContext({ sr_recent_miss_examples: {} }, 'create')).not.toContain(
      'Concrete misses from the last cycle',
    );
    expect(
      formatProjectContext({ sr_recent_miss_examples: { correctness: [] } }, 'create'),
    ).not.toContain('Concrete misses from the last cycle');
  });

  it('renders the canonical contract fixture (cross-language contract with backend)', () => {
    // Loads the same fixture that backend/testing/test_agent.py asserts the claim
    // endpoint produces. If either side drifts from this shape, both tests must update.
    const fixturePath = join(__dirname, '__fixtures__', 'sr_recent_miss_examples_contract.json');
    const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as {
      sr_recent_miss_examples: Record<string, Array<{ severity: string; desc: string }>>;
    };
    const ctx = { sr_recent_miss_examples: fixture.sr_recent_miss_examples };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('## Concrete misses from the last cycle');
    expect(result).toContain('**correctness:**');
    expect(result).toContain('[CRITICAL] Off-by-one in loop boundary check');
    expect(result).toContain('**security:**');
    expect(result).toContain('[MAJOR] User input passed directly to SQL query');
  });

  it('does NOT render from the legacy sr_catch_rate_trends shape (no key/shape collision)', () => {
    // Guards against contract regression — the trends doc shape must not be misread as miss examples.
    const ctx = {
      sr_catch_rate_trends: {
        categories: {
          security: {
            miss_rate: 0.9,
            examples: [{ severity: 'CRITICAL', desc: 'should not render' }],
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Concrete misses from the last cycle');
    expect(result).not.toContain('should not render');
  });
});

describe('formatProjectContext — SR vs Fresh-Reviewer Disagreements with examples', () => {
  it('renders verbatim examples nested under each disagreement entry', () => {
    const ctx = {
      sr_reviewer_disagreements: {
        window_days: 30,
        disagreements: [
          {
            category: 'correctness',
            task_type: 'create',
            count: 3,
            examples: [
              { severity: 'HIGH', description: 'Off-by-one error in loop boundary' },
              { severity: 'MEDIUM', description: 'Null pointer not checked before dereference' },
            ],
          },
          {
            category: 'security',
            task_type: 'create',
            count: 1,
            examples: [
              { severity: 'CRITICAL', description: 'User input passed directly to SQL query' },
            ],
          },
        ],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('### SR vs Fresh-Reviewer Disagreements');
    expect(result).toContain('**correctness**: 3 disagreements');
    expect(result).toContain('[HIGH] Off-by-one error in loop boundary');
    expect(result).toContain('[MEDIUM] Null pointer not checked before dereference');
    expect(result).toContain('**security**: 1 disagreement');
    expect(result).toContain('[CRITICAL] User input passed directly to SQL query');
  });

  it('renders count-only when entries have no examples', () => {
    const ctx = {
      sr_reviewer_disagreements: {
        window_days: 14,
        disagreements: [
          { category: 'type-safety', task_type: 'create', count: 2 },
        ],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('**type-safety**: 2 disagreements');
  });

  it('caps examples at 3 per entry', () => {
    const ctx = {
      sr_reviewer_disagreements: {
        window_days: 30,
        disagreements: [
          {
            category: 'correctness',
            task_type: 'create',
            count: 5,
            examples: [
              { severity: 'HIGH', description: 'Example one' },
              { severity: 'HIGH', description: 'Example two' },
              { severity: 'MEDIUM', description: 'Example three' },
              { severity: 'LOW', description: 'Example four — should not appear' },
            ],
          },
        ],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Example one');
    expect(result).toContain('Example two');
    expect(result).toContain('Example three');
    expect(result).not.toContain('Example four — should not appear');
  });

  it('adds task-type scope note to section header', () => {
    const ctx = {
      sr_reviewer_disagreements: {
        window_days: 14,
        disagreements: [{ category: 'correctness', task_type: 'create', count: 2 }],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('especially in create tasks');
  });

  it('appends task-type suffix on cross-type entries', () => {
    const ctx = {
      sr_reviewer_disagreements: {
        window_days: 14,
        disagreements: [
          { category: 'correctness', task_type: 'fix', count: 1 },
        ],
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('**correctness**: 1 disagreement (fix tasks)');
  });

  it('omits the section when sr_reviewer_disagreements is absent or empty', () => {
    expect(formatProjectContext({}, 'create')).not.toContain('SR vs Fresh-Reviewer Disagreements');
    expect(
      formatProjectContext({ sr_reviewer_disagreements: { disagreements: [] } }, 'create'),
    ).not.toContain('SR vs Fresh-Reviewer Disagreements');
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

describe('formatProjectContext — Failure-Mode Hints', () => {
  it('renders failure-mode hints when a regressing category has miss_mode_breakdown', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          correctness: {
            miss_rate: 0.4,
            trend: 'decreasing',
            miss_mode_breakdown: {
              reading_gap: 8,
              judgment_gap: 2,
              coverage_gap: 1,
              dominant_mode: 'reading_gap',
            },
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('Failure-mode hints for regressing categories');
    expect(result).toContain('correctness');
    expect(result).toContain('reading_gap');
    expect(result).toContain('re-read all changed files before scoring');
  });

  it('omits the failure-mode section when no category has miss_mode_breakdown', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          correctness: { miss_rate: 0.4, trend: 'decreasing' },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Failure-mode hints for regressing categories');
  });

  it('omits the failure-mode section when no category is regressing', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          correctness: {
            miss_rate: 0.2,
            miss_mode_breakdown: {
              reading_gap: 2,
              judgment_gap: 1,
              coverage_gap: 0,
              dominant_mode: 'reading_gap',
            },
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('Failure-mode hints for regressing categories');
  });

  it('places failure-mode hints after blind-spots section', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          correctness: {
            miss_rate: 0.4,
            trend: 'decreasing',
            miss_mode_breakdown: {
              reading_gap: 5,
              judgment_gap: 1,
              coverage_gap: 0,
              dominant_mode: 'reading_gap',
            },
          },
        },
      },
    };
    const result = formatProjectContext(ctx, 'create');
    const blindIdx = result.indexOf('Self-Review Blind Spots');
    const hintIdx = result.indexOf('Failure-mode hints');
    expect(blindIdx).toBeGreaterThanOrEqual(0);
    expect(hintIdx).toBeGreaterThan(blindIdx);
  });
});

describe('formatProjectContext — Persistent Self-Review Blind Spots', () => {
  it('renders persistent blind-spot section when data is present', () => {
    const ctx = {
      persistent_blind_spots: {
        categories: [
          { category: 'correctness', task_type: 'create', rate: 0.15 },
          { category: 'error-handling', task_type: 'create', rate: 0.12 },
        ],
        hint: 'Fresh reviewer has caught issues in these categories across 2+ consecutive windows. Apply extra scrutiny.',
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).toContain('## Persistent Self-Review Blind Spots');
    expect(result).toContain('Fresh reviewer has caught issues in these categories across 2+ consecutive windows');
  });

  it('omits persistent blind-spot section when hint is absent', () => {
    const ctx = {
      persistent_blind_spots: {
        categories: [
          { category: 'correctness', task_type: 'create', rate: 0.15 },
        ],
        hint: null,
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('## Persistent Self-Review Blind Spots');
  });

  it('omits persistent blind-spot section when persistent_blind_spots is absent', () => {
    const ctx = {};
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('## Persistent Self-Review Blind Spots');
  });

  it('omits persistent blind-spot section when categories array is empty', () => {
    const ctx = {
      persistent_blind_spots: {
        categories: [],
        hint: 'Some hint text',
      },
    };
    const result = formatProjectContext(ctx, 'create');
    expect(result).not.toContain('## Persistent Self-Review Blind Spots');
  });

  it('places persistent blind-spot section after failure-mode hints', () => {
    const ctx = {
      self_review_catch_rate: {
        categories: {
          correctness: {
            miss_rate: 0.4,
            trend: 'decreasing',
            miss_mode_breakdown: {
              reading_gap: 5,
              judgment_gap: 1,
              coverage_gap: 0,
              dominant_mode: 'reading_gap',
            },
          },
        },
      },
      persistent_blind_spots: {
        categories: [
          { category: 'security', task_type: 'create', rate: 0.10 },
        ],
        hint: 'Persistent security issues detected',
      },
    };
    const result = formatProjectContext(ctx, 'create');
    const failureModeIdx = result.indexOf('Failure-mode hints');
    const persistentIdx = result.indexOf('## Persistent Self-Review Blind Spots');
    expect(failureModeIdx).toBeGreaterThanOrEqual(0);
    expect(persistentIdx).toBeGreaterThan(failureModeIdx);
  });
});
