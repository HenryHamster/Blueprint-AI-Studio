import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ParkWakeHealthCard } from './ParkWakeHealthCard';
import type { ParkWakeHealth } from '../types/digest';

function makeData(overrides: Partial<ParkWakeHealth> = {}): ParkWakeHealth {
  return {
    parked_task_count: 3,
    oldest_parked_age_seconds: 7200,
    fired_undelivered_count: 2,
    oldest_fired_age_seconds: 900,
    delivery_stalled: false,
    delivery_stall_threshold_seconds: 900,
    ...overrides,
  };
}

describe('ParkWakeHealthCard', () => {
  it('renders parked population, ages, backlog, and healthy delivery state', () => {
    render(<ParkWakeHealthCard data={makeData()} />);
    expect(screen.getByText('Park/Wake Health')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('currently parked')).toBeInTheDocument();
    expect(screen.getByText('2h')).toBeInTheDocument();
    expect(screen.getByText('15m')).toBeInTheDocument();
    expect(screen.getByText('Fired, undelivered')).toBeInTheDocument();
    expect(screen.getByText('Wake delivery flowing')).toBeInTheDocument();
  });

  it('shows dash labels for missing ages', () => {
    render(
      <ParkWakeHealthCard
        data={makeData({ parked_task_count: 0, oldest_parked_age_seconds: null, fired_undelivered_count: 0, oldest_fired_age_seconds: null })}
      />,
    );
    expect(screen.getAllByText('—')).toHaveLength(2);
  });

  it('renders the stalled delivery warning and threshold', () => {
    render(<ParkWakeHealthCard data={makeData({ delivery_stalled: true, delivery_stall_threshold_seconds: 1800 })} />);
    expect(screen.getByText(/Warning:/)).toBeInTheDocument();
    expect(screen.getByText(/Delivery stalled past 30m/)).toBeInTheDocument();
  });

  it('renders nothing when data is null and stale is false', () => {
    const { container } = render(<ParkWakeHealthCard data={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a greyed placeholder when data is null and stale is true', () => {
    render(<ParkWakeHealthCard data={null} stale={true} />);
    expect(screen.getByText('Park/Wake Health')).toBeInTheDocument();
    expect(screen.getByText('Data unavailable')).toBeInTheDocument();
  });

  it('renders the full card even when stale is true and data is present', () => {
    render(<ParkWakeHealthCard data={makeData()} stale={true} />);
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('currently parked')).toBeInTheDocument();
  });
});
