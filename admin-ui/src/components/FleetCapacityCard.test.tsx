import { describe, test, expect } from 'vitest';
import { render } from '@testing-library/react';
import { FleetCapacityCard } from './FleetCapacityCard';
import type { FleetUtilizationResponse } from '../api';

function makeSnapshot(overrides: Partial<FleetUtilizationResponse> = {}): FleetUtilizationResponse {
  return {
    active_now: 3,
    nominal_capacity: 10,
    usable_capacity: 8,
    cooled_accounts: 1,
    util_now: 0.3,
    util_avg: 0.4,
    window_hours: 24,
    saturation: 'healthy',
    ...overrides,
  };
}

describe('FleetCapacityCard', () => {
  test('renders saturation badge for healthy status', () => {
    const { getByText } = render(
      <FleetCapacityCard data={makeSnapshot({ saturation: 'healthy' })} loading={false} />,
    );
    expect(getByText('Healthy')).toBeTruthy();
  });

  test('renders busy saturation badge', () => {
    const { getByText } = render(
      <FleetCapacityCard data={makeSnapshot({ saturation: 'busy' })} loading={false} />,
    );
    expect(getByText('Busy')).toBeTruthy();
  });

  test('renders saturated saturation badge', () => {
    const { getByText } = render(
      <FleetCapacityCard data={makeSnapshot({ saturation: 'saturated' })} loading={false} />,
    );
    expect(getByText('Saturated')).toBeTruthy();
  });

  test('renders Unknown badge when saturation is null', () => {
    const { getByText } = render(
      <FleetCapacityCard data={makeSnapshot({ saturation: null, nominal_capacity: null, util_avg: null, util_now: null })} loading={false} />,
    );
    expect(getByText('Unknown')).toBeTruthy();
  });

  test('shows active slots and nominal capacity', () => {
    const { getByText } = render(
      <FleetCapacityCard data={makeSnapshot({ active_now: 5, nominal_capacity: 20 })} loading={false} />,
    );
    expect(getByText('5 / 20')).toBeTruthy();
  });

  test('shows utilization percentages', () => {
    const { getByText } = render(
      <FleetCapacityCard data={makeSnapshot({ util_avg: 0.75, util_now: 0.3 })} loading={false} />,
    );
    expect(getByText('75%')).toBeTruthy();
    expect(getByText('30%')).toBeTruthy();
  });

  test('shows cooldown note when cooled accounts exist', () => {
    const { getByText } = render(
      <FleetCapacityCard data={makeSnapshot({ cooled_accounts: 2 })} loading={false} />,
    );
    expect(getByText(/rate-limited/)).toBeTruthy();
  });

  test('shows skeleton while loading', () => {
    const { container } = render(<FleetCapacityCard data={null} loading={true} />);
    expect(container.querySelector('.animate-pulse')).toBeTruthy();
  });

  test('shows unavailable message when data is null and not loading', () => {
    const { getByText } = render(<FleetCapacityCard data={null} loading={false} />);
    expect(getByText(/unavailable/i)).toBeTruthy();
  });
});
