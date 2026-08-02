import { describe, it, expect } from 'vitest';
import { formatSrBlindSpotThemesForSrPrompt, formatExecutionEvidenceForSrPrompt, formatSrRecentMissExamplesForSrPrompt, formatHighFpRateLoosenHintForSrPrompt, formatSrCoFailurePairsForSrPrompt, formatSeverityCalibrationHintForSrPrompt, formatPrecisionRecallQuadrantHintForSrPrompt } from './self-review.js';

describe('formatHighFpRateLoosenHintForSrPrompt', () => {
  it('returns empty string for null, undefined, and empty object', () => {
    expect(formatHighFpRateLoosenHintForSrPrompt(null)).toBe('');
    expect(formatHighFpRateLoosenHintForSrPrompt(undefined)).toBe('');
    expect(formatHighFpRateLoosenHintForSrPrompt({})).toBe('');
  });

  it('returns empty string when fp_rate is below 0.5 threshold', () => {
    expect(formatHighFpRateLoosenHintForSrPrompt({ correctness: { false_positive_rate: 0.4, sr_flag_sample_size: 20 } })).toBe('');
  });

  it('returns empty string when sample_size is below 10 threshold', () => {
    expect(formatHighFpRateLoosenHintForSrPrompt({ correctness: { false_positive_rate: 0.8, sr_flag_sample_size: 9 } })).toBe('');
  });

  it('emits section with category name and fp_rate when both thresholds are met', () => {
    const result = formatHighFpRateLoosenHintForSrPrompt({
      'scope-drift': { false_positive_rate: 0.68, sr_flag_sample_size: 15 },
    });
    expect(result).toContain('## Over-flagged categories');
    expect(result).toContain('scope-drift');
    expect(result).toContain('68%');
    expect(result).toContain('n=15');
  });

  it('includes categories above threshold and excludes those below', () => {
    const result = formatHighFpRateLoosenHintForSrPrompt({
      'scope-drift': { false_positive_rate: 0.68, sr_flag_sample_size: 15 },
      correctness: { false_positive_rate: 0.3, sr_flag_sample_size: 20 },
    });
    expect(result).toContain('scope-drift');
    expect(result).not.toContain('correctness');
  });

  it('ends with double newline when populated', () => {
    const result = formatHighFpRateLoosenHintForSrPrompt({
      correctness: { false_positive_rate: 0.6, sr_flag_sample_size: 12 },
    });
    expect(result.endsWith('\n\n')).toBe(true);
  });

  it('includes category at exact fp_rate=0.5 boundary', () => {
    const result = formatHighFpRateLoosenHintForSrPrompt({
      correctness: { false_positive_rate: 0.5, sr_flag_sample_size: 10 },
    });
    expect(result).toContain('correctness');
    expect(result).toContain('50%');
  });

  it('excludes category at exact fp_rate=0.49 (just below boundary)', () => {
    const result = formatHighFpRateLoosenHintForSrPrompt({
      correctness: { false_positive_rate: 0.49, sr_flag_sample_size: 20 },
    });
    expect(result).toBe('');
  });

  it('excludes category at exact sample_size=9 (just below boundary)', () => {
    const result = formatHighFpRateLoosenHintForSrPrompt({
      correctness: { false_positive_rate: 0.9, sr_flag_sample_size: 9 },
    });
    expect(result).toBe('');
  });

  it('instructs agent to require concrete evidence before flagging', () => {
    const result = formatHighFpRateLoosenHintForSrPrompt({
      correctness: { false_positive_rate: 0.6, sr_flag_sample_size: 12 },
    });
    expect(result).toContain('concrete');
    expect(result).toContain('evidence');
  });
});
describe('formatPrecisionRecallQuadrantHintForSrPrompt', () => {
  it('returns empty string for null, undefined, and empty object', () => {
    expect(formatPrecisionRecallQuadrantHintForSrPrompt(null)).toBe('');
    expect(formatPrecisionRecallQuadrantHintForSrPrompt(undefined)).toBe('');
    expect(formatPrecisionRecallQuadrantHintForSrPrompt({})).toBe('');
  });

  it('returns empty string when no categories have a quadrant field', () => {
    expect(
      formatPrecisionRecallQuadrantHintForSrPrompt({
        correctness: { precision_delta: 0.1, recall_delta: -0.1 },
      }),
    ).toBe('');
  });

  it('excludes healthy, flat, and mixed quadrants', () => {
    const result = formatPrecisionRecallQuadrantHintForSrPrompt({
      a: { precision_recall_quadrant: 'healthy', precision_delta: 0.1, recall_delta: 0.1 },
      b: { precision_recall_quadrant: 'flat', precision_delta: 0.0, recall_delta: 0.0 },
      c: { precision_recall_quadrant: 'mixed', precision_delta: 0.1, recall_delta: 0.0 },
    });
    expect(result).toBe('');
  });

  it('emits conservative_shift guidance with "lean toward flagging"', () => {
    const result = formatPrecisionRecallQuadrantHintForSrPrompt({
      correctness: { precision_recall_quadrant: 'conservative_shift', precision_delta: 0.02, recall_delta: -0.15 },
    });
    expect(result).toContain('## Precision/recall directional calibration');
    expect(result).toContain('correctness');
    expect(result).toContain('precision held, recall down');
    expect(result).toContain('15pp');
    expect(result).toContain('lean toward flagging');
    expect(result).toContain('under-flagging');
  });

  it('emits noisy_shift guidance with "stronger, concrete evidence"', () => {
    const result = formatPrecisionRecallQuadrantHintForSrPrompt({
      'scope-drift': { precision_recall_quadrant: 'noisy_shift', precision_delta: -0.20, recall_delta: 0.03 },
    });
    expect(result).toContain('scope-drift');
    expect(result).toContain('recall held, precision down');
    expect(result).toContain('20pp');
    expect(result).toContain('concrete evidence');
    expect(result).toContain('not being confirmed');
  });

  it('emits both_declining guidance to recalibrate from scratch', () => {
    const result = formatPrecisionRecallQuadrantHintForSrPrompt({
      security: { precision_recall_quadrant: 'both_declining', precision_delta: -0.12, recall_delta: -0.18 },
    });
    expect(result).toContain('security');
    expect(result).toContain('both precision and recall down');
    expect(result).toContain('12pp');
    expect(result).toContain('18pp');
    expect(result).toContain('Recalibrate from scratch');
  });

  it('surfaces both deltas in pp format', () => {
    const result = formatPrecisionRecallQuadrantHintForSrPrompt({
      correctness: { precision_recall_quadrant: 'conservative_shift', precision_delta: 0.05, recall_delta: -0.22 },
    });
    expect(result).toContain('Δprecision');
    expect(result).toContain('Δrecall');
    expect(result).toContain('22pp');
    expect(result).toContain('5pp');
  });

  it('omits categories whose quadrant field is missing while including those that match', () => {
    const result = formatPrecisionRecallQuadrantHintForSrPrompt({
      correctness: { precision_recall_quadrant: 'conservative_shift', precision_delta: 0.01, recall_delta: -0.20 },
      'no-quadrant': { precision_delta: 0.5, recall_delta: -0.5 },
      healthy: { precision_recall_quadrant: 'healthy', precision_delta: 0.10, recall_delta: 0.10 },
    });
    expect(result).toContain('correctness');
    expect(result).not.toContain('no-quadrant');
    expect(result).not.toContain('healthy:');
  });

  it('ranks rows by max(|precision_delta|, |recall_delta|) descending and caps at 5', () => {
    const cats: Record<string, { precision_recall_quadrant?: string; precision_delta?: number; recall_delta?: number }> = {};
    // 6 categories — top one is `big` with recall_delta=-0.50
    for (let i = 0; i < 6; i++) {
      cats[`cat${i}`] = { precision_recall_quadrant: 'conservative_shift', precision_delta: 0.01, recall_delta: -(0.05 + i * 0.01) };
    }
    cats['big'] = { precision_recall_quadrant: 'conservative_shift', precision_delta: 0.01, recall_delta: -0.50 };
    const result = formatPrecisionRecallQuadrantHintForSrPrompt(cats);
    expect(result).toContain('big');
    expect(result).toContain('50pp');
    // Exactly 5 bullet rows (lines starting with '- **')
    const rowLines = result.split('\n').filter(l => l.startsWith('- **'));
    expect(rowLines.length).toBe(5);
  });

  it('ends with double newline when populated', () => {
    const result = formatPrecisionRecallQuadrantHintForSrPrompt({
      correctness: { precision_recall_quadrant: 'conservative_shift', precision_delta: 0.02, recall_delta: -0.15 },
    });
    expect(result.endsWith('\n\n')).toBe(true);
  });
});
describe('formatExecutionEvidenceForSrPrompt', () => {
  it('includes all three command-execution category names', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('tests_executed_cleanly');
    expect(result).toContain('lint_executed_cleanly');
    expect(result).toContain('typecheck_executed_cleanly');
  });

  it('includes test_assertion_quality sub-check', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('test_assertion_quality');
  });

  it('describes test_assertion_quality as checking assertion specificity vs smoke', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('smoke');
    expect(result).toContain('specific behavior changed');
  });

  it('describes severity levels for test_assertion_quality', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('"minor"');
    expect(result).toContain('"major"');
  });

  it('instructs agent to use severity=major for command failures', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('severity="major"');
  });

  it('describes evidence field format', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('exit_code=0 from <command>');
  });

  it('makes clear that claiming pass without execution log is a FAIL', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('no execution log');
    expect(result).toContain('FAIL');
  });

  it('returns non-empty string unconditionally', () => {
    expect(formatExecutionEvidenceForSrPrompt().length).toBeGreaterThan(0);
  });

  it('scopes test suite guidance to agent VM constraints: targeted/standalone backend tests', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('agent VMs');
    expect(result).toContain('testing/test_<module>.py');
    expect(result).toContain('--standalone');
    expect(result).toContain('--no-postgres');
  });

  it('explicitly forbids running the full backend suite on agent VMs', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('NEVER the full backend suite');
    expect(result).toContain('OOMs');
    expect(result).toContain('CLAUDE.md');
  });

  it('specifies frontend test commands for agent VMs', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('npm run typecheck');
  });

  it('specifies agent/TypeScript test commands', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('npx tsc --noEmit');
  });

  it('includes full backend suite violation in FAIL rules', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('Running the full backend suite');
    expect(result).toContain('violates CLAUDE.md');
  });
});

describe('formatSrRecentMissExamplesForSrPrompt', () => {
  it('returns empty string for null', () => {
    expect(formatSrRecentMissExamplesForSrPrompt(null)).toBe('');
  });

  it('returns empty string for undefined', () => {
    expect(formatSrRecentMissExamplesForSrPrompt(undefined)).toBe('');
  });

  it('returns empty string when input is empty', () => {
    expect(formatSrRecentMissExamplesForSrPrompt({})).toBe('');
  });

  it('returns empty string when all categories have no examples', () => {
    expect(formatSrRecentMissExamplesForSrPrompt({
      correctness: [],
      security: [],
    })).toBe('');
  });

  it('renders section heading and category with examples', () => {
    const result = formatSrRecentMissExamplesForSrPrompt({
      correctness: [{ severity: 'HIGH', desc: 'missed null check in handler' }],
    });
    expect(result).toContain('## Concrete misses from the last cycle');
    expect(result).toContain('**correctness:**');
    expect(result).toContain('[HIGH] missed null check in handler');
  });

  it('instructs heightened scrutiny', () => {
    const result = formatSrRecentMissExamplesForSrPrompt({
      security: [{ severity: 'CRITICAL', desc: 'SQL injection via user input' }],
    });
    expect(result).toContain('heightened scrutiny');
    expect(result).toContain('similar code patterns');
  });

  it('preserves backend-supplied category ordering and caps at 3', () => {
    // Backend pre-ranks by weighted_miss_rate, so insertion order encodes priority.
    const result = formatSrRecentMissExamplesForSrPrompt({
      cat_high: [{ severity: 'HIGH', desc: 'high miss' }],
      cat_med: [{ severity: 'MEDIUM', desc: 'med miss' }],
      cat_low: [{ severity: 'LOW', desc: 'low miss' }],
      cat_extra: [{ severity: 'LOW', desc: 'extra miss' }],
    });
    expect(result).toContain('**cat_high:**');
    expect(result).toContain('**cat_med:**');
    expect(result).toContain('**cat_low:**');
    expect(result).not.toContain('**cat_extra:**');
  });

  it('caps examples per category at 3', () => {
    const result = formatSrRecentMissExamplesForSrPrompt({
      correctness: [
        { severity: 'HIGH', desc: 'miss 1' },
        { severity: 'HIGH', desc: 'miss 2' },
        { severity: 'HIGH', desc: 'miss 3' },
        { severity: 'HIGH', desc: 'miss 4' },
      ],
    });
    expect(result).toContain('miss 1');
    expect(result).toContain('miss 3');
    expect(result).not.toContain('miss 4');
  });

  it('truncates long descriptions to 200 chars with ellipsis', () => {
    const longDesc = 'x'.repeat(250);
    const result = formatSrRecentMissExamplesForSrPrompt({
      correctness: [{ severity: 'HIGH', desc: longDesc }],
    });
    expect(result).toContain('x'.repeat(200) + '…');
    expect(result).not.toContain('x'.repeat(201));
  });

  it('renders multiple categories as separate sections', () => {
    const result = formatSrRecentMissExamplesForSrPrompt({
      security: [{ severity: 'CRITICAL', desc: 'injection' }],
      correctness: [{ severity: 'HIGH', desc: 'off-by-one' }],
    });
    expect(result).toContain('**security:**');
    expect(result).toContain('**correctness:**');
    expect(result).toContain('[CRITICAL] injection');
    expect(result).toContain('[HIGH] off-by-one');
  });
});

describe('formatSrBlindSpotThemesForSrPrompt', () => {
  it('returns empty string for empty array', () => {
    expect(formatSrBlindSpotThemesForSrPrompt([])).toBe('');
  });

  it('returns empty string for null', () => {
    expect(formatSrBlindSpotThemesForSrPrompt(null)).toBe('');
  });

  it('returns empty string for undefined', () => {
    expect(formatSrBlindSpotThemesForSrPrompt(undefined)).toBe('');
  });

  it('renders themes with agent-directed heading and framing', () => {
    const result = formatSrBlindSpotThemesForSrPrompt([
      { theme: 'did not run full docker pytest', count: 3 },
      { theme: 'missed null guard', count: 1 },
    ]);
    expect(result).toContain('## Your historical blind spots');
    expect(result).toContain('historically missed issues');
    expect(result).toContain('extra skepticism');
    expect(result).toContain('did not run full docker pytest (3 tasks)');
    expect(result).toContain('missed null guard (1 task)');
  });

  it('uses singular "task" for count of 1', () => {
    const result = formatSrBlindSpotThemesForSrPrompt([{ theme: 'foo', count: 1 }]);
    expect(result).toContain('(1 task)');
    expect(result).not.toContain('(1 tasks)');
  });

  it('uses plural "tasks" for count > 1', () => {
    const result = formatSrBlindSpotThemesForSrPrompt([{ theme: 'bar', count: 5 }]);
    expect(result).toContain('(5 tasks)');
  });

  it('caps output at 8 themes', () => {
    const themes = Array.from({ length: 12 }, (_, i) => ({
      theme: `theme ${i}`,
      count: 12 - i,
    }));
    const result = formatSrBlindSpotThemesForSrPrompt(themes);
    expect(result).toContain('theme 0');
    expect(result).toContain('theme 7');
    expect(result).not.toContain('theme 8');
    expect(result).not.toContain('theme 11');
  });
});

describe('formatSrCoFailurePairsForSrPrompt', () => {
  it('returns empty string for null', () => {
    expect(formatSrCoFailurePairsForSrPrompt(null)).toBe('');
  });

  it('returns empty string for undefined', () => {
    expect(formatSrCoFailurePairsForSrPrompt(undefined)).toBe('');
  });

  it('returns empty string for empty array', () => {
    expect(formatSrCoFailurePairsForSrPrompt([])).toBe('');
  });

  it('returns empty string when all pairs below lift threshold', () => {
    const result = formatSrCoFailurePairsForSrPrompt([
      { cat_a: 'correctness', cat_b: 'test-coverage', lift: 1.2 },
      { cat_a: 'security', cat_b: 'error-handling', lift: 1.1 },
    ]);
    expect(result).toBe('');
  });

  it('emits section with pairs above lift threshold', () => {
    const result = formatSrCoFailurePairsForSrPrompt([
      { cat_a: 'correctness', cat_b: 'test-coverage', lift: 2.3 },
    ]);
    expect(result).toContain('## Co-failure pairs');
    expect(result).toContain('correctness');
    expect(result).toContain('test-coverage');
    expect(result).toContain('2.3x');
  });

  it('includes pairs at exact lift=1.5 boundary', () => {
    const result = formatSrCoFailurePairsForSrPrompt([
      { cat_a: 'type-safety', cat_b: 'regression_risk', lift: 1.5 },
    ]);
    expect(result).toContain('type-safety');
    expect(result).toContain('regression_risk');
  });

  it('excludes pairs below lift=1.5 boundary', () => {
    const result = formatSrCoFailurePairsForSrPrompt([
      { cat_a: 'style-conventions', cat_b: 'documentation', lift: 1.49 },
    ]);
    expect(result).toBe('');
  });

  it('includes both above-threshold and excludes below-threshold pairs', () => {
    const result = formatSrCoFailurePairsForSrPrompt([
      { cat_a: 'correctness', cat_b: 'test-coverage', lift: 2.5 },
      { cat_a: 'style-conventions', cat_b: 'documentation', lift: 1.2 },
      { cat_a: 'security', cat_b: 'error-handling', lift: 3.1 },
    ]);
    expect(result).toContain('correctness');
    expect(result).toContain('test-coverage');
    expect(result).toContain('security');
    expect(result).not.toContain('style-conventions');
  });

  it('caps output at 5 pairs', () => {
    const pairs = Array.from({ length: 8 }, (_, i) => ({
      cat_a: `cat_a_${i}`,
      cat_b: `cat_b_${i}`,
      lift: 2.0 + i * 0.1,
    }));
    const result = formatSrCoFailurePairsForSrPrompt(pairs);
    expect(result).toContain('cat_a_0');
    expect(result).toContain('cat_b_4');
    expect(result).not.toContain('cat_a_5');
  });

  it('stays under ~800 chars even with many pairs', () => {
    const pairs = Array.from({ length: 10 }, (_, i) => ({
      cat_a: `very_long_category_name_${i}`,
      cat_b: `another_very_long_category_name_${i}`,
      lift: 2.5 + i * 0.1,
    }));
    const result = formatSrCoFailurePairsForSrPrompt(pairs);
    expect(result.length).toBeLessThan(850);
  });

  it('instructs agent to re-check partner before marking PASS', () => {
    const result = formatSrCoFailurePairsForSrPrompt([
      { cat_a: 'correctness', cat_b: 'test-coverage', lift: 2.0 },
    ]);
    expect(result).toContain('re-check');
    expect(result).toContain('before marking PASS');
  });
});

describe('formatSeverityCalibrationHintForSrPrompt', () => {
  it('returns empty string for null', () => {
    expect(formatSeverityCalibrationHintForSrPrompt(null)).toBe('');
  });

  it('returns empty string for undefined', () => {
    expect(formatSeverityCalibrationHintForSrPrompt(undefined)).toBe('');
  });

  it('returns empty string when input is empty object', () => {
    expect(formatSeverityCalibrationHintForSrPrompt({})).toBe('');
  });

  it('returns empty string when no category meets 0.30 ratio threshold', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 1, over_rated: 0, matched: 9, total: 10 },
      },
    });
    expect(result).toBe('');
  });

  it('returns empty string when total is below 5 samples threshold', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 2, over_rated: 0, matched: 1, total: 3 },
      },
    });
    expect(result).toBe('');
  });

  it('includes heading when under_rated threshold is met', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 3, over_rated: 0, matched: 2, total: 5 },
      },
    });
    expect(result).toContain('## Severity calibration tightening');
    expect(result).toContain('under-rated');
  });

  it('includes heading when over_rated threshold is met', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 0, over_rated: 3, matched: 2, total: 5 },
      },
    });
    expect(result).toContain('## Severity calibration tightening');
    expect(result).toContain('over-rated');
  });

  it('surfaces both under_rated and over_rated when both thresholds met', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 3, over_rated: 0, matched: 2, total: 5 },
      },
      security: {
        severity_calibration: { under_rated: 0, over_rated: 2, matched: 3, total: 5 },
      },
    });
    expect(result).toContain('under-rated');
    expect(result).toContain('over-rated');
    expect(result).toContain('correctness');
    expect(result).toContain('security');
  });

  it('caps to top 3 under_rated categories by ratio', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      cat1: { severity_calibration: { under_rated: 4, over_rated: 0, matched: 1, total: 5 } },
      cat2: { severity_calibration: { under_rated: 3, over_rated: 0, matched: 2, total: 5 } },
      cat3: { severity_calibration: { under_rated: 2, over_rated: 0, matched: 3, total: 5 } },
      cat4: { severity_calibration: { under_rated: 2, over_rated: 0, matched: 3, total: 5 } },
    });
    expect(result).toContain('cat1');
    expect(result).toContain('cat2');
    expect(result).toContain('cat3');
    expect(result).not.toContain('cat4');
  });

  it('includes percentage and sample size for under_rated category', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 3, over_rated: 0, matched: 2, total: 10 },
      },
    });
    expect(result).toContain('30%');
    expect(result).toContain('n=10');
  });

  it('rounds percentages correctly', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 3, over_rated: 0, matched: 7, total: 10 },
      },
    });
    expect(result).toContain('30%');
  });

  it('ends with double newline when populated', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 3, over_rated: 0, matched: 2, total: 5 },
      },
    });
    expect(result.endsWith('\n\n')).toBe(true);
  });

  it('ignores categories without severity_calibration', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: { severity_calibration: { under_rated: 3, over_rated: 0, matched: 2, total: 5 } },
      security: {},
    });
    expect(result).toContain('correctness');
    expect(result).not.toContain('security');
  });

  it('includes LEAN toward MAJOR for under_rated guidance', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 3, over_rated: 0, matched: 2, total: 5 },
      },
    });
    expect(result).toContain('MAJOR');
  });

  it('includes LEAN toward MINOR for over_rated guidance', () => {
    const result = formatSeverityCalibrationHintForSrPrompt({
      correctness: {
        severity_calibration: { under_rated: 0, over_rated: 3, matched: 2, total: 5 },
      },
    });
    expect(result).toContain('MINOR');
  });
});
