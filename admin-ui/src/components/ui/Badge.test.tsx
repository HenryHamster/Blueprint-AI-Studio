import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { Badge, StatusToneBadge, VerdictBadge } from './Badge';
import { statusBadgeClasses } from '../../utils/projectStatus';
import type { StatusTone } from '../../utils/projectStatus';

const TONES: StatusTone[] = ['success', 'blocking', 'warning', 'info', 'neutral'];

describe('Badge', () => {
  it('renders a <span> by default', () => {
    const { container } = render(<Badge tone="success">ok</Badge>);
    expect(container.querySelector('span')).not.toBeNull();
    expect(container.querySelector('button')).toBeNull();
  });

  it('renders a <button> when onClick is provided', () => {
    const { container } = render(<Badge tone="neutral" onClick={() => {}}>click</Badge>);
    expect(container.querySelector('button')).not.toBeNull();
    expect(container.querySelector('span')).toBeNull();
  });

  it.each(TONES)('applies statusBadgeClasses for tone=%s', (tone) => {
    const { container } = render(<Badge tone={tone}>label</Badge>);
    const el = container.firstElementChild as HTMLElement;
    const expected = statusBadgeClasses(tone);
    for (const cls of expected.split(' ')) {
      expect(el.className).toContain(cls);
    }
  });

  it('applies custom className on top of tone classes', () => {
    const { container } = render(<Badge tone="success" className="gap-1 font-semibold">x</Badge>);
    const el = container.firstElementChild as HTMLElement;
    expect(el.className).toContain('gap-1');
    expect(el.className).toContain('font-semibold');
    expect(el.className).toContain('status-success');
  });

  it('renders without tone using only className', () => {
    const { container } = render(<Badge className="bg-red-500 text-white">raw</Badge>);
    const el = container.firstElementChild as HTMLElement;
    expect(el.className).toContain('bg-red-500');
    expect(el.className).not.toContain('status-');
  });

  it('forwards title and aria-label', () => {
    const { container } = render(
      <Badge tone="warning" title="tip" aria-label="badge label">warn</Badge>,
    );
    const el = container.firstElementChild as HTMLElement;
    expect(el.getAttribute('title')).toBe('tip');
    expect(el.getAttribute('aria-label')).toBe('badge label');
  });
});

describe('StatusToneBadge', () => {
  it('renders label with positive tone classes', () => {
    const { getByText } = render(<StatusToneBadge tone="positive" label="Approve" />);
    const el = getByText('Approve');
    expect(el.className).toContain('text-status-success');
    expect(el.className).toContain('bg-status-success');
  });

  it('renders label with blocking tone classes', () => {
    const { getByText } = render(<StatusToneBadge tone="blocking" label="Fail" />);
    const el = getByText('Fail');
    expect(el.className).toContain('text-status-blocking');
    expect(el.className).toContain('bg-status-blocking');
  });

  it('renders label with neutral tone classes', () => {
    const { getByText } = render(<StatusToneBadge tone="neutral" label="Undetected" />);
    const el = getByText('Undetected');
    expect(el.className).toContain('text-status-neutral');
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
    expect(el.className).toContain('text-status-success');
  });

  it('maps request_changes verdict to Request Changes label with blocking tone', () => {
    const { getByText } = render(<VerdictBadge verdict="request_changes" />);
    const el = getByText('Request Changes');
    expect(el.className).toContain('text-status-blocking');
  });

  it('maps unknown verdict to title-cased label with info tone', () => {
    const { getByText } = render(<VerdictBadge verdict="some_custom_verdict" />);
    getByText('Some Custom Verdict');
  });
});
