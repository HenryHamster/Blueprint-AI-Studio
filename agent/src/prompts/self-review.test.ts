import { describe, it, expect } from 'vitest';
import { formatSrBlindSpotThemesForSrPrompt, formatExecutionEvidenceForSrPrompt } from './self-review.js';

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
