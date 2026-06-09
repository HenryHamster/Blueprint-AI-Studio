import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StrategicChallengeChip } from './StrategicChallengeChip';

describe('StrategicChallengeChip', () => {
  it('renders nothing when summary is absent (legacy cycle)', () => {
    const { container } = render(<StrategicChallengeChip summary={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when summary is undefined', () => {
    const { container } = render(<StrategicChallengeChip summary={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders an approved verdict with emerald (non-warning) styling and no counts', () => {
    render(
      <StrategicChallengeChip
        summary={{ mode: 'ok', verdict: 'approved', warned_count: 0, blocked_count: 0 }}
      />,
    );
    const chip = screen.getByLabelText(/strategic judgment challenge/i);
    expect(chip).toHaveTextContent('challenge: ok · approved');
    expect(chip.className).toContain('emerald');
    expect(chip.className).not.toContain('red');
    expect(chip.className).not.toContain('amber');
    // Approved-clean chip shows no count clause.
    expect(chip.textContent).not.toContain('warned');
    expect(chip.textContent).not.toContain('blocked');
  });

  it('renders a warned verdict in amber with the warned count', () => {
    render(
      <StrategicChallengeChip
        summary={{ mode: 'ok', verdict: 'warned', warned_count: 2, blocked_count: 0 }}
      />,
    );
    const chip = screen.getByLabelText(/strategic judgment challenge/i);
    expect(chip).toHaveTextContent('challenge: ok · warned');
    expect(chip).toHaveTextContent('2 warned');
    expect(chip.className).toContain('amber');
  });

  it('renders a blocked verdict in red with both blocked and warned counts', () => {
    render(
      <StrategicChallengeChip
        summary={{ mode: 'blocked', verdict: 'blocked', warned_count: 1, blocked_count: 3 }}
      />,
    );
    const chip = screen.getByLabelText(/strategic judgment challenge/i);
    expect(chip).toHaveTextContent('challenge: blocked · blocked');
    expect(chip).toHaveTextContent('3 blocked');
    expect(chip).toHaveTextContent('1 warned');
    expect(chip.className).toContain('red');
  });

  it('defaults verdict to approved when omitted and tolerates missing counts', () => {
    render(<StrategicChallengeChip summary={{ mode: 'ok' }} />);
    const chip = screen.getByLabelText(/strategic judgment challenge/i);
    expect(chip).toHaveTextContent('challenge: ok · approved');
    expect(chip.textContent).not.toContain('warned');
  });

  it('surfaces non-ok modes even when no counts are present', () => {
    render(<StrategicChallengeChip summary={{ mode: 'invalid', verdict: 'blocked' }} />);
    const chip = screen.getByLabelText(/strategic judgment challenge/i);
    expect(chip).toHaveTextContent('challenge: invalid · blocked');
    expect(chip.textContent).not.toContain('warned');
    expect(chip.textContent).not.toContain('blocked blocked');
  });
});
