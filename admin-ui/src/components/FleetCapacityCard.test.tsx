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
    vm_cap: 10,
    active_task_vms: 3,
    managed_vm_count: 6,
    idle_pool_vms: 1,
    vm_slots_available: 5,
    vm_saturation: 'healthy',
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
    const { getAllByText } = render(
      <FleetCapacityCard data={makeSnapshot({ saturation: 'healthy' })} loading={false} />,
    );
    expect(getAllByText('Healthy')).toHaveLength(2);
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

  test('marks and dims stale snapshots when backend served a stale fallback', () => {
    const { container, getByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({
          precomputed: true,
          precomputed_at: '2024-01-01T00:00:00',
          precompute_age_seconds: 30,
          served_stale: true,
        })}
        loading={false}
      />,
    );
    expect(getByText(/as of .*stale/)).toBeTruthy();
    expect(container.querySelector('.opacity-60.saturate-50')).toBeTruthy();
  });

  test('marks cached snapshots stale when their age exceeds the threshold', () => {
    const { getByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({
          precomputed: true,
          precomputed_at: '2024-01-01T00:00:00',
          precompute_age_seconds: 121,
        })}
        loading={false}
      />,
    );
    expect(getByText(/as of .*stale/)).toBeTruthy();
  });

  test('renders older fleet payloads without a freshness affordance', () => {
    const { container, getByText, queryByText } = render(
      <FleetCapacityCard data={makeSnapshot({ active_now: 5, nominal_capacity: 20 })} loading={false} />,
    );
    expect(queryByText(/as of/)).toBeNull();
    expect(container.querySelector('.opacity-60.saturate-50')).toBeNull();
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

  test('shows VM slot utilization values', () => {
    const { getByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({
          vm_cap: 12,
          active_task_vms: 4,
          managed_vm_count: 9,
          idle_pool_vms: 2,
          vm_slots_available: 3,
          vm_saturation: 'busy',
        })}
        loading={false}
      />,
    );
    expect(getByText('VM Slots')).toBeTruthy();
    expect(getByText('12')).toBeTruthy();
    expect(getByText('4')).toBeTruthy();
    expect(getByText('9')).toBeTruthy();
    expect(getByText('2')).toBeTruthy();
    expect(getByText(/3 · Busy/)).toBeTruthy();
  });

  test('escalates header badge when VM slots are saturated', () => {
    const { getAllByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({ saturation: 'healthy', vm_saturation: 'saturated', vm_slots_available: 0 })}
        loading={false}
      />,
    );
    expect(getAllByText('Saturated')).toHaveLength(2);
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

  test('renders unknown affordance for null managed_vm_count distinct from zero', () => {
    const { getAllByText, queryByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({ managed_vm_count: null, vm_slots_available: null, vm_saturation: null })}
        loading={false}
      />,
    );
    // "unknown" should appear for null counts, not a plain "0"
    const unknownEls = getAllByText('unknown');
    expect(unknownEls.length).toBeGreaterThanOrEqual(1);
    // A bare "0" for managed_vm_count should not be rendered
    expect(queryByText('0')).toBeNull();
  });

  test('renders unknown affordance for null idle_pool_vms distinct from zero', () => {
    const { getAllByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({ idle_pool_vms: null, vm_slots_available: null, vm_saturation: null })}
        loading={false}
      />,
    );
    expect(getAllByText('unknown').length).toBeGreaterThanOrEqual(1);
  });

  test('renders zero explicitly when pool is disabled (idle_pool_vms=0, managed_vm_count known)', () => {
    const { getByText, queryAllByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({ idle_pool_vms: 0, managed_vm_count: 4, vm_slots_available: 6, vm_saturation: 'healthy' })}
        loading={false}
      />,
    );
    // zero idle pool VMs should show as literal "0", not "unknown"
    expect(getByText('0')).toBeTruthy();
    expect(queryAllByText('unknown').length).toBe(0);
  });

  test('shows measurements-unavailable note when vm_cap is set but vm_saturation is null', () => {
    const { getByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({
          managed_vm_count: null,
          idle_pool_vms: null,
          vm_slots_available: null,
          vm_saturation: null,
        })}
        loading={false}
      />,
    );
    expect(getByText(/VM measurements partially unavailable/)).toBeTruthy();
  });

  test('does not show measurements-unavailable note when all VM counts are known', () => {
    const { queryByText } = render(
      <FleetCapacityCard
        data={makeSnapshot({ managed_vm_count: 5, idle_pool_vms: 1, vm_slots_available: 4, vm_saturation: 'healthy' })}
        loading={false}
      />,
    );
    expect(queryByText(/VM measurements partially unavailable/)).toBeNull();
  });
});
