import { describe, it, expect } from 'vitest';
import {
  formatVerdict,
  verdictToneBadgeClass,
} from './verdictFormatting';

describe('formatVerdict', () => {
  it('maps panel_review approve to a positive (emerald) badge with a human label', () => {
    const { label, tone } = formatVerdict('approve');
    expect(label).toBe('Approve');
    expect(tone).toBe('positive');
    expect(verdictToneBadgeClass(tone)).toContain('emerald');
  });

  it('maps panel_review request_changes to a blocking (red) badge with a human label', () => {
    const { label, tone } = formatVerdict('request_changes');
    expect(label).toBe('Request Changes');
    expect(tone).toBe('blocking');
    expect(verdictToneBadgeClass(tone)).toContain('red');
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
    expect(verdictToneBadgeClass(formatVerdict('undetected').tone)).toContain('neutral');
  });

  it('selects info (cyan) styling for unknown tokens', () => {
    expect(verdictToneBadgeClass(formatVerdict('something_else').tone)).toContain('cyan');
  });

  it('verdictToneBadgeClass reuses the canonical class strings', () => {
    expect(verdictToneBadgeClass('positive')).toBe('bg-emerald-900/40 text-emerald-300 border-emerald-800/40');
    expect(verdictToneBadgeClass('blocking')).toBe('bg-red-900/40 text-red-300 border-red-800/40');
  });
});
