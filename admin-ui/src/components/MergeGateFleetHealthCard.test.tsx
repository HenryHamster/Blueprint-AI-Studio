import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MergeGateFleetHealthCard } from './MergeGateFleetHealthCard';
import type { MergeGateFleetHealth } from '../types/digest';

function makeData(overrides: Partial<MergeGateFleetHealth> = {}): MergeGateFleetHealth {
  return {
    verified_count: 3,
    fell_open_count: 1,
    verification_rate: 0.75,
    most_common_reason: 'probe_passed',
    degraded_project_count: 1,
    project_count: 2,
    ...overrides,
  };
}

describe('MergeGateFleetHealthCard', () => {
  it('renders verification rate, counts, and dominant fall-open reason', () => {
    render(<MergeGateFleetHealthCard data={makeData()} />);
    expect(screen.getByText('75%')).toBeInTheDocument();
    expect(screen.getByText(/across 2 projects/)).toBeInTheDocument();
    expect(screen.getByText('3 verified')).toBeInTheDocument();
    expect(screen.getByText('1 fell open')).toBeInTheDocument();
    expect(screen.getByText('1 degraded')).toBeInTheDocument();
    // Reason key is humanized via the shared label map, not shown raw.
    expect(screen.queryByText('probe_passed')).not.toBeInTheDocument();
    expect(screen.getByText(/cheap acceptance probe/)).toBeInTheDocument();
  });

  it('renders nothing when data is null and stale is false', () => {
    const { container } = render(<MergeGateFleetHealthCard data={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a greyed placeholder when data is null and stale is true', () => {
    render(<MergeGateFleetHealthCard data={null} stale={true} />);
    expect(screen.getByText('Merge Gate Health')).toBeInTheDocument();
    expect(screen.getByText('Data unavailable')).toBeInTheDocument();
  });

  it('renders the full card even when stale is true and data is present', () => {
    render(<MergeGateFleetHealthCard data={makeData()} stale={true} />);
    expect(screen.getByText('75%')).toBeInTheDocument();
    expect(screen.getByText(/across 2 projects/)).toBeInTheDocument();
  });

  it('renders nothing when project_count is 0 even if stale is true', () => {
    const { container } = render(
      <MergeGateFleetHealthCard
        data={makeData({ project_count: 0, verified_count: 0, fell_open_count: 0, verification_rate: null, most_common_reason: null, degraded_project_count: 0 })}
        stale={true}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when there is no gate activity (project_count 0)', () => {
    const { container } = render(
      <MergeGateFleetHealthCard
        data={makeData({ project_count: 0, verified_count: 0, fell_open_count: 0, verification_rate: null, most_common_reason: null, degraded_project_count: 0 })}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('hides the fall-open reason and degraded count for a fully-verified fleet', () => {
    render(
      <MergeGateFleetHealthCard
        data={makeData({ verified_count: 4, fell_open_count: 0, verification_rate: 1, most_common_reason: null, degraded_project_count: 0 })}
      />,
    );
    expect(screen.getByText('100%')).toBeInTheDocument();
    expect(screen.queryByText(/degraded/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Top fall-open reason/)).not.toBeInTheDocument();
  });
});
