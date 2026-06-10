import { describe, test, expect } from 'vitest';
import { render } from '@testing-library/react';
import { FleetCapacityCard } from './FleetCapacityCard';
import type { AgentServingShasResponse, FleetUtilizationResponse } from '../api';

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

function makeAgentServing(overrides: Partial<AgentServingShasResponse> = {}): AgentServingShasResponse {
  return {
    fleet: {
      total_observed: 2,
      unknown_count: 0,
      mixed: false,
      buckets: [{
        source_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        count: 2,
        warm_pool_count: 1,
        task_vm_count: 1,
        image_names: ['agent-image-a'],
        latest_build_ts: 1781049600,
        last_heartbeat_at: null,
        matches_backend_served: true,
        matches_current_image: true,
      }],
    },
    current_image: {
      image_name: 'agent-image-a',
      source_git_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      build_ts: 1781049600,
    },
    backend_served_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    active_candidate_sha: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    integration_branch: 'automain',
    integration_head_sha: 'cccccccccccccccccccccccccccccccccccccccc',
    promotion_target: 'main',
    promotion_head_sha: null,
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

  test('renders matching agent image serving SHAs', () => {
    const { getAllByText, getByText } = render(
      <FleetCapacityCard
        data={makeSnapshot()}
        agentServingShas={makeAgentServing()}
        loading={false}
      />,
    );
    expect(getByText('Agent Image')).toBeTruthy();
    expect(getAllByText('aaaaaaaaaaaa').length).toBeGreaterThanOrEqual(2);
    expect(getByText('backend')).toBeTruthy();
    expect(getByText('image')).toBeTruthy();
  });

  test('renders mixed rollover and unknown pre-marker bucket', () => {
    const { getAllByText, getByText } = render(
      <FleetCapacityCard
        data={makeSnapshot()}
        agentServingShas={makeAgentServing({
          fleet: {
            total_observed: 3,
            unknown_count: 1,
            mixed: true,
            buckets: [
              {
                source_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                count: 1,
                warm_pool_count: 1,
                task_vm_count: 0,
                image_names: [],
                latest_build_ts: null,
                last_heartbeat_at: null,
              },
              {
                source_sha: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                count: 1,
                warm_pool_count: 0,
                task_vm_count: 1,
                image_names: [],
                latest_build_ts: null,
                last_heartbeat_at: null,
              },
              {
                source_sha: null,
                count: 1,
                warm_pool_count: 1,
                task_vm_count: 0,
                image_names: [],
                latest_build_ts: null,
                last_heartbeat_at: null,
              },
            ],
          },
        })}
        loading={false}
      />,
    );
    expect(getByText('Mixed rollover')).toBeTruthy();
    expect(getByText('pre-marker')).toBeTruthy();
    expect(getAllByText('1 VM').length).toBeGreaterThanOrEqual(3);
  });

  test('does not render agent image section when serving data is absent', () => {
    const { queryByText } = render(
      <FleetCapacityCard data={makeSnapshot()} agentServingShas={null} loading={false} />,
    );
    expect(queryByText('Agent Image')).toBeNull();
  });
});
