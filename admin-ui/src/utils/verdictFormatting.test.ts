import { describe, it, expect } from 'vitest';
import {
  formatVerdict,
  verdictToneBadgeClass,
} from './verdictFormatting';

describe('formatVerdict', () => {
  it('maps approve verdict to a positive (success) badge with a human label', () => {
    const { label, tone } = formatVerdict('approve');
    expect(label).toBe('Approve');
    expect(tone).toBe('positive');
    expect(verdictToneBadgeClass(tone)).toContain('status-success');
  });

  it('maps request_changes verdict to a blocking badge with a human label', () => {
    const { label, tone } = formatVerdict('request_changes');
    expect(label).toBe('Request Changes');
    expect(tone).toBe('blocking');
    expect(verdictToneBadgeClass(tone)).toContain('status-blocking');
  });

  it('is case-insensitive (REQUEST_CHANGES never leaks the raw token)', () => {
    const { label, tone } = formatVerdict('REQUEST_CHANGES');
    expect(label).toBe('Request Changes');
    expect(tone).toBe('blocking');
  });

  it('maps the other evaluator verdicts to the right tone and label', () => {
    expect(formatVerdict('pass')).toEqual({ label: 'Pass', tone: 'positive' });
    expect(formatVerdict('fail')).toEqual({ label: 'Fail', tone: 'blocking' });
    expect(formatVerdict('actionable')).toEqual({ label: 'Actionable', tone: 'blocking' });
    expect(formatVerdict('clean')).toEqual({ label: 'Clean', tone: 'positive' });
    expect(formatVerdict('undetected')).toEqual({ label: 'Undetected', tone: 'neutral' });
    expect(formatVerdict('recorded')).toEqual({ label: 'Recorded', tone: 'info' });
  });

  it('falls back to Title Case for unknown tokens (never renders snake_case)', () => {
    const { label, tone } = formatVerdict('needs_followup');
    expect(label).toBe('Needs Followup');
    expect(tone).toBe('info');
    expect(label).not.toContain('_');
  });

  it('returns an empty label for an empty verdict', () => {
    expect(formatVerdict('')).toEqual({ label: '', tone: 'info' });
  });
});

describe('verdictTone / badge classes', () => {
  it('selects neutral styling for undetected', () => {
    expect(formatVerdict('undetected').tone).toBe('neutral');
    expect(verdictToneBadgeClass(formatVerdict('undetected').tone)).toContain('status-neutral');
  });

  it('selects info (blue) styling for unknown tokens', () => {
    expect(verdictToneBadgeClass(formatVerdict('something_else').tone)).toContain('status-info');
  });

  it('verdictToneBadgeClass delegates to the canonical token-backed mapper', () => {
    expect(verdictToneBadgeClass('positive')).toBe('bg-status-success/15 text-status-success border border-status-success/20');
    expect(verdictToneBadgeClass('blocking')).toBe('bg-status-blocking/15 text-status-blocking border border-status-blocking/20');
  });
});
