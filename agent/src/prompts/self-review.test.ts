import { describe, it, expect } from 'vitest';
import { formatSrBlindSpotThemesForSrPrompt, formatExecutionEvidenceForSrPrompt, formatSrRecentMissExamplesForSrPrompt } from './self-review.js';

describe('formatExecutionEvidenceForSrPrompt', () => {
  it('includes all three category names', () => {
    const result = formatExecutionEvidenceForSrPrompt();
    expect(result).toContain('tests_executed_cleanly');
    expect(result).toContain('lint_executed_cleanly');
    expect(result).toContain('typecheck_executed_cleanly');
  });

  it('instructs agent to use severity=major for failures', () => {
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
