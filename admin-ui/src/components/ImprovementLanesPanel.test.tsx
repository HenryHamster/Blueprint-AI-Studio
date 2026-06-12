import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ImprovementLanesPanel } from './ImprovementLanesPanel';

const STORAGE_KEY = 'blueprint-admin:improvementLanesPanelCollapsed';

const fetchImprovementLanes = vi.fn();
const drainLanePilot = vi.fn();

vi.mock('../api', () => ({
  fetchImprovementLanes: (...args: unknown[]) => fetchImprovementLanes(...args),
  drainLanePilot: (...args: unknown[]) => drainLanePilot(...args),
}));

const LANE_RESPONSE = {
  lanes: [
    {
      id: 'lane-1',
      status: 'ACTIVE',
      direction_id: 'd-123',
      branch: 'lane/branch-a',
      last_checkpoint_phase: 'review',
      last_checkpoint_seq: 3,
      last_checkpoint_sha: 'ccc333',
      last_checkpoint_age_seconds: 90,
      checkpoint_loss_count: 0,
      workspace_state: 'WARM_RESERVED',
      workspace_vm_instance_id: 'vm-warm-1',
      reservation_expires_at: '2026-06-11T12:00:00Z',
      reserved_vm_instance_id: 'vm-warm-1',
      pilot_arm: 'lane',
      phase_requeues: 1,
      llm_input_tokens_total: 500,
      llm_output_tokens_total: 250,
      vm_cost_usd_total: 0.12,
      reserved_idle_cost_usd_total: 0.03,
      shadow: false,
      created_at: '2026-06-11T10:00:00Z',
      updated_at: '2026-06-11T11:00:00Z',
    },
    {
      id: 'lane-2',
      status: 'PARKED',
      direction_id: 'd-456',
      branch: 'lane/branch-b',
      last_checkpoint_phase: 'fix',
      last_checkpoint_seq: 2,
      last_checkpoint_sha: 'bbb222',
      last_checkpoint_age_seconds: 3600,
      checkpoint_loss_count: 2,
      workspace_state: 'COLD',
      workspace_vm_instance_id: null,
      reservation_expires_at: null,
      reserved_vm_instance_id: null,
      pilot_arm: null,
      phase_requeues: 0,
      llm_input_tokens_total: 0,
      llm_output_tokens_total: 0,
      vm_cost_usd_total: 0,
      reserved_idle_cost_usd_total: 0,
      shadow: true,
      created_at: '2026-06-11T09:00:00Z',
      updated_at: '2026-06-11T10:30:00Z',
    },
  ],
  total_returned: 2,
  offset: 0,
  limit: 50,
  project_id: 'proj-1',
};

describe('ImprovementLanesPanel', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    fetchImprovementLanes.mockReset();
    drainLanePilot.mockReset();
    localStorage.clear();
  });

  test('renders lane rows and labels shadow-mode lanes', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    expect(rows).toHaveLength(2);
    expect(screen.getByText('lane/branch-a')).toBeTruthy();
    expect(screen.getByText('lane/branch-b')).toBeTruthy();
    expect(screen.getByText('Shadow mode')).toBeTruthy();
    expect(screen.getByText('WARM_RESERVED')).toBeTruthy();
    expect(screen.getByText('Phase requeues 1')).toBeTruthy();
    expect(screen.getAllByText('Reserved idle / loss')).toHaveLength(2);
    expect(screen.getByText('$0.03 / 0')).toBeTruthy();
    expect(screen.getByTestId('improvement-lanes-shadow-summary').textContent).toBe('1 shadow');
    expect(screen.getByTestId('improvement-lane-shadow-badge')).toBeTruthy();
  });

  test('shows a loading banner while the fetch is pending', async () => {
    let resolveFetch!: (value: unknown) => void;
    fetchImprovementLanes.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );

    render(<ImprovementLanesPanel projectId="proj-1" />);

    expect(await screen.findByText('Loading improvement lanes…')).toBeTruthy();
    resolveFetch(LANE_RESPONSE);
    expect(await screen.findAllByTestId('improvement-lane-row')).toHaveLength(2);
  });

  test('persists collapse preference across remounts', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);

    const { unmount } = render(<ImprovementLanesPanel projectId="proj-1" />);
    const rows = await screen.findAllByTestId('improvement-lane-row');
    expect(rows).toHaveLength(2);

    fireEvent.click(screen.getByTestId('improvement-lanes-toggle'));

    expect(screen.queryAllByTestId('improvement-lane-row')).toHaveLength(0);
    expect(localStorage.getItem(STORAGE_KEY)).toBe('true');
    unmount();

    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    render(<ImprovementLanesPanel projectId="proj-1" />);
    await waitFor(() => expect(screen.getByText('2 lanes')).toBeTruthy());
    expect(screen.queryAllByTestId('improvement-lane-row')).toHaveLength(0);
  });

  test('renders an error state when the fetch fails', async () => {
    fetchImprovementLanes.mockRejectedValueOnce(new Error('boom'));

    render(<ImprovementLanesPanel projectId="proj-1" />);

    expect(await screen.findByText('boom')).toBeTruthy();
    await waitFor(() => expect(screen.queryAllByTestId('improvement-lane-row')).toHaveLength(0));
  });

  test('renders Drain/Kill Pilot button in expanded panel', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    await screen.findAllByTestId('improvement-lane-row');
    expect(screen.getByTestId('drain-lane-pilot-button')).toBeTruthy();
    expect(screen.getByTestId('drain-lane-pilot-button').textContent).toBe('Drain / Kill Pilot');
  });

  test('shows ConfirmDialog when Drain/Kill Pilot button is clicked', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(screen.getByTestId('drain-lane-pilot-button'));

    expect(await screen.findByText('Drain / Kill Lane Pilot')).toBeTruthy();
    expect(screen.getByText(/stops all future lane assignment/)).toBeTruthy();
  });

  test('dismisses ConfirmDialog on cancel', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(screen.getByTestId('drain-lane-pilot-button'));
    expect(await screen.findByText('Drain / Kill Lane Pilot')).toBeTruthy();

    fireEvent.click(screen.getByText('Cancel'));
    await waitFor(() => expect(screen.queryByText('Drain / Kill Lane Pilot')).toBeNull());
    expect(drainLanePilot).not.toHaveBeenCalled();
  });

  test('calls drainLanePilot and displays result summary on confirm', async () => {
    fetchImprovementLanes.mockResolvedValue(LANE_RESPONSE);
    drainLanePilot.mockResolvedValueOnce({
      project_id: 'proj-1',
      flag_disabled: true,
      reservations_cleared: 2,
      lanes_killed: 1,
      reason: 'operator_drain',
    });

    render(<ImprovementLanesPanel projectId="proj-1" />);

    await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(screen.getByTestId('drain-lane-pilot-button'));
    expect(await screen.findByText('Drain / Kill Lane Pilot')).toBeTruthy();

    fireEvent.click(screen.getByText('Drain & Kill'));

    expect(await screen.findByTestId('drain-result-summary')).toBeTruthy();
    expect(screen.getByTestId('drain-result-summary').textContent).toBe(
      'Pilot drained: 2 reservations released, 1 lane killed',
    );
    expect(drainLanePilot).toHaveBeenCalledWith('proj-1');
  });

  test('shows error banner on drain failure', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    drainLanePilot.mockRejectedValueOnce(new Error('drain failed'));

    render(<ImprovementLanesPanel projectId="proj-1" />);

    await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(screen.getByTestId('drain-lane-pilot-button'));
    fireEvent.click(await screen.findByText('Drain & Kill'));

    expect(await screen.findByText('drain failed')).toBeTruthy();
  });
});
