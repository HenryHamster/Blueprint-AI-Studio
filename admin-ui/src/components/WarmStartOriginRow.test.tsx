import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { WarmStartOriginRow } from './WarmStartOriginRow';

const breakdown = {
  warm_pool: 3,
  backlog_reuse: 2,
  chain_continuity: 5,
  warm_total: 10,
  pool_origin_share: 0.3,
};

describe('WarmStartOriginRow', () => {
  it('renders pool/backlog/chain counts and pool-share % when breakdown provided', () => {
    render(<WarmStartOriginRow breakdown={breakdown} />);
    const row = screen.getByTestId('warm-start-origin-breakdown');
    expect(row.textContent).toContain('warm by origin');
    expect(row.textContent).toContain('3');
    expect(row.textContent).toContain('2');
    expect(row.textContent).toContain('5');
    expect(screen.getByTestId('warm-start-pool-share').textContent).toContain('30%');
  });

  it('renders nothing when breakdown is null', () => {
    const { container } = render(<WarmStartOriginRow breakdown={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when breakdown is undefined', () => {
    const { container } = render(<WarmStartOriginRow breakdown={undefined} />);
    expect(container.firstChild).toBeNull();
  });
});
