import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { StatusToneBadge, VerdictBadge } from './Badge';

describe('StatusToneBadge', () => {
  it('renders label with positive tone classes', () => {
    const { getByText } = render(<StatusToneBadge tone="positive" label="Approve" />);
    const el = getByText('Approve');
    expect(el.className).toContain('text-emerald');
    expect(el.className).toContain('bg-emerald');
  });

  it('renders label with blocking tone classes', () => {
    const { getByText } = render(<StatusToneBadge tone="blocking" label="Fail" />);
    const el = getByText('Fail');
    expect(el.className).toContain('text-red');
    expect(el.className).toContain('bg-red');
  });

  it('renders label with neutral tone classes', () => {
    const { getByText } = render(<StatusToneBadge tone="neutral" label="Undetected" />);
    const el = getByText('Undetected');
    expect(el.className).toContain('text-neutral');
  });

  it('includes base badge layout classes', () => {
    const { getByText } = render(<StatusToneBadge tone="info" label="Info" />);
    const el = getByText('Info');
    expect(el.className).toContain('inline-flex');
    expect(el.className).toContain('rounded');
    expect(el.className).toContain('border');
  });
});

describe('VerdictBadge', () => {
  it('maps approve verdict to Approve label with positive tone', () => {
    const { getByText } = render(<VerdictBadge verdict="approve" />);
    const el = getByText('Approve');
    expect(el.className).toContain('text-emerald');
  });

  it('maps request_changes verdict to Request Changes label with blocking tone', () => {
    const { getByText } = render(<VerdictBadge verdict="request_changes" />);
    const el = getByText('Request Changes');
    expect(el.className).toContain('text-red');
  });

  it('maps unknown verdict to title-cased label with info tone', () => {
    const { getByText } = render(<VerdictBadge verdict="some_custom_verdict" />);
    getByText('Some Custom Verdict');
  });
});
