import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ImprovementLanesPanel } from './ImprovementLanesPanel';

const STORAGE_KEY = 'blueprint-admin:improvementLanesPanelCollapsed';

const fetchImprovementLanes = vi.fn();
const drainLanePilot = vi.fn();
const fetchLanePilotPreflight = vi.fn().mockResolvedValue(null);
const fetchImprovementLaneDetail = vi.fn();

vi.mock('../api', () => ({
  fetchImprovementLanes: (...args: unknown[]) => fetchImprovementLanes(...args),
  drainLanePilot: (...args: unknown[]) => drainLanePilot(...args),
  fetchLanePilotPreflight: (...args: unknown[]) => fetchLanePilotPreflight(...args),
  fetchImprovementLaneDetail: (...args: unknown[]) => fetchImprovementLaneDetail(...args),
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
      reservation_claimed_by_fix_count: 3,
      reservation_expired_before_fix_count: 1,
      reservation_force_expired_count: 0,
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
      reservation_claimed_by_fix_count: 0,
      reservation_expired_before_fix_count: 0,
      reservation_force_expired_count: 0,
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
    fetchLanePilotPreflight.mockReset();
    fetchLanePilotPreflight.mockResolvedValue(null);
    fetchImprovementLaneDetail.mockReset();
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

  test('renders shadow health rollup block when shadow_health is present', async () => {
    const responseWithHealth = {
      ...LANE_RESPONSE,
      shadow_health: {
        shadow_lane_count: 1,
        checkpoints_written: 4,
        checkpoint_miss_count: 1,
        exhausted_close_count: 0,
        miss_rate: 0.2,
      },
    };
    fetchImprovementLanes.mockResolvedValueOnce(responseWithHealth);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    await screen.findAllByTestId('improvement-lane-row');
    expect(screen.getByTestId('shadow-lane-health')).toBeTruthy();
    expect(screen.getByTestId('shadow-lane-health').textContent).toContain('Shadow checkpoint health');
    expect(screen.getByTestId('shadow-lane-health').textContent).toContain('20.0%');
    // Miss-rate badge also appears in the header
    expect(screen.getByTestId('improvement-lanes-shadow-miss-rate').textContent).toBe('miss 20%');
  });

  test('renders warm-resume engagement breakdown for lane with non-zero claimed count', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    // lane-1 has claimed=3, expired=1, force=0
    const breakdown = rows[0].querySelector('[data-testid="reservation-cause-breakdown"]');
    expect(breakdown).toBeTruthy();
    expect(breakdown?.textContent).toContain('Claimed');
    expect(breakdown?.textContent).toContain('3');
    expect(breakdown?.textContent).toContain('Expired early');
    expect(breakdown?.textContent).toContain('1');
    expect(breakdown?.textContent).toContain('Force-expired');

    // claimed > 0 → emerald color class
    const claimedSpan = breakdown?.querySelector('[class*="emerald"]');
    expect(claimedSpan).toBeTruthy();

    // expired > 0 → amber color class
    const expiredSpan = breakdown?.querySelector('[class*="amber"]');
    expect(expiredSpan).toBeTruthy();
  });

  test('shows expired reservation in amber when expiry is in the past', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    // lane-1 has reservation_expires_at: '2026-06-11T12:00:00Z' which is in the past
    const expiry = rows[0].querySelector('[data-testid="reservation-expiry"]');
    expect(expiry).toBeTruthy();
    expect(expiry?.textContent).toMatch(/^Expired /);
    expect(expiry?.className).toContain('amber');
  });

  test('shows live reservation in neutral styling when expiry is in the future', async () => {
    const futureExpiry = new Date(Date.now() + 60 * 60 * 1000).toISOString();
    const responseWithFutureReservation = {
      ...LANE_RESPONSE,
      lanes: [
        { ...LANE_RESPONSE.lanes[0], reservation_expires_at: futureExpiry },
        LANE_RESPONSE.lanes[1],
      ],
    };
    fetchImprovementLanes.mockResolvedValueOnce(responseWithFutureReservation);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    const expiry = rows[0].querySelector('[data-testid="reservation-expiry"]');
    expect(expiry).toBeTruthy();
    expect(expiry?.textContent).toMatch(/^Expires /);
    expect(expiry?.className).toContain('neutral');
    expect(expiry?.className).not.toContain('amber');
  });

  test('renders cold lane reservation breakdown without failure styling when all counts are zero', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    // lane-2 has all zeros — should not display emerald/amber/red classes for values
    const breakdown = rows[1].querySelector('[data-testid="reservation-cause-breakdown"]');
    expect(breakdown).toBeTruthy();
    expect(breakdown?.textContent).toContain('0');
    // no amber or red spans for the count values (they are all zero → neutral styling)
    const amberOrRedSpans = breakdown?.querySelectorAll('[class*="amber"], [class*="red"]');
    expect(amberOrRedSpans?.length ?? 0).toBe(0);
  });

  test('renders correct badge classes for all five lane statuses', async () => {
    const ALL_STATUSES_RESPONSE = {
      lanes: [
        { id: 'l-proposed', status: 'PROPOSED', direction_id: 'd-1', branch: 'lane/proposed', last_checkpoint_phase: null, last_checkpoint_seq: null, last_checkpoint_sha: null, last_checkpoint_age_seconds: null, checkpoint_loss_count: 0, workspace_state: null, workspace_vm_instance_id: null, reservation_expires_at: null, reserved_vm_instance_id: null, pilot_arm: null, phase_requeues: 0, llm_input_tokens_total: 0, llm_output_tokens_total: 0, vm_cost_usd_total: 0, reserved_idle_cost_usd_total: 0, shadow: false, created_at: '2026-06-11T10:00:00Z', updated_at: '2026-06-11T10:00:00Z' },
        { id: 'l-active', status: 'ACTIVE', direction_id: 'd-2', branch: 'lane/active', last_checkpoint_phase: null, last_checkpoint_seq: null, last_checkpoint_sha: null, last_checkpoint_age_seconds: null, checkpoint_loss_count: 0, workspace_state: null, workspace_vm_instance_id: null, reservation_expires_at: null, reserved_vm_instance_id: null, pilot_arm: null, phase_requeues: 0, llm_input_tokens_total: 0, llm_output_tokens_total: 0, vm_cost_usd_total: 0, reserved_idle_cost_usd_total: 0, shadow: false, created_at: '2026-06-11T10:00:00Z', updated_at: '2026-06-11T10:00:00Z' },
        { id: 'l-parked', status: 'PARKED', direction_id: 'd-3', branch: 'lane/parked', last_checkpoint_phase: null, last_checkpoint_seq: null, last_checkpoint_sha: null, last_checkpoint_age_seconds: null, checkpoint_loss_count: 0, workspace_state: null, workspace_vm_instance_id: null, reservation_expires_at: null, reserved_vm_instance_id: null, pilot_arm: null, phase_requeues: 0, llm_input_tokens_total: 0, llm_output_tokens_total: 0, vm_cost_usd_total: 0, reserved_idle_cost_usd_total: 0, shadow: false, created_at: '2026-06-11T10:00:00Z', updated_at: '2026-06-11T10:00:00Z' },
        { id: 'l-retired', status: 'RETIRED', direction_id: 'd-4', branch: 'lane/retired', last_checkpoint_phase: null, last_checkpoint_seq: null, last_checkpoint_sha: null, last_checkpoint_age_seconds: null, checkpoint_loss_count: 0, workspace_state: null, workspace_vm_instance_id: null, reservation_expires_at: null, reserved_vm_instance_id: null, pilot_arm: null, phase_requeues: 0, llm_input_tokens_total: 0, llm_output_tokens_total: 0, vm_cost_usd_total: 0, reserved_idle_cost_usd_total: 0, shadow: false, created_at: '2026-06-11T10:00:00Z', updated_at: '2026-06-11T10:00:00Z' },
        { id: 'l-killed', status: 'KILLED', direction_id: 'd-5', branch: 'lane/killed', last_checkpoint_phase: null, last_checkpoint_seq: null, last_checkpoint_sha: null, last_checkpoint_age_seconds: null, checkpoint_loss_count: 0, workspace_state: null, workspace_vm_instance_id: null, reservation_expires_at: null, reserved_vm_instance_id: null, pilot_arm: null, phase_requeues: 0, llm_input_tokens_total: 0, llm_output_tokens_total: 0, vm_cost_usd_total: 0, reserved_idle_cost_usd_total: 0, shadow: false, created_at: '2026-06-11T10:00:00Z', updated_at: '2026-06-11T10:00:00Z' },
      ],
      total_returned: 5,
      offset: 0,
      limit: 50,
      project_id: 'proj-1',
    };

    fetchImprovementLanes.mockResolvedValueOnce(ALL_STATUSES_RESPONSE);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    expect(rows).toHaveLength(5);

    const badges = rows.map((row) =>
      row.querySelector('[class*="rounded"][class*="border"][class*="px-1.5"][class*="uppercase"]'),
    );

    // PROPOSED — slate neutral-pending
    expect(badges[0]?.className).toContain('text-slate-300');
    expect(badges[0]?.textContent).toBe('PROPOSED');

    // ACTIVE — emerald
    expect(badges[1]?.className).toContain('text-emerald-300');
    expect(badges[1]?.textContent).toBe('ACTIVE');

    // PARKED — amber
    expect(badges[2]?.className).toContain('text-amber-300');
    expect(badges[2]?.textContent).toBe('PARKED');

    // RETIRED — sky/teal completed look
    expect(badges[3]?.className).toContain('text-sky-300');
    expect(badges[3]?.textContent).toBe('RETIRED');

    // KILLED — red failure look
    expect(badges[4]?.className).toContain('text-red-300');
    expect(badges[4]?.textContent).toBe('KILLED');
  });

  test('lane with null last_checkpoint_sha displays "none" for checkpoint age', async () => {
    const noCheckpointResponse = {
      ...LANE_RESPONSE,
      lanes: [
        {
          ...LANE_RESPONSE.lanes[0],
          last_checkpoint_sha: null,
          last_checkpoint_age_seconds: null,
        },
      ],
    };
    fetchImprovementLanes.mockResolvedValueOnce(noCheckpointResponse);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    await screen.findAllByTestId('improvement-lane-row');
    // Header age span shows 'none' (exact text)
    const noneElements = screen.getAllByText('none');
    expect(noneElements.length).toBeGreaterThanOrEqual(1);
    // Detail "Age" line shows 'Age none', not 'Age —'
    const ageDetail = screen.getByText(/^Age /);
    expect(ageDetail.textContent).toBe('Age none');
  });

  test('shows preflight error indicator when preflight fetch fails', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchLanePilotPreflight.mockRejectedValueOnce(new Error('Network error'));

    render(<ImprovementLanesPanel projectId="proj-1" />);

    expect(await screen.findByText('Preflight data unavailable')).toBeTruthy();
    expect(screen.queryByTestId('lane-pilot-preflight')).toBeNull();
  });

  test('does not show error indicator when preflight returns null (no pilot configured)', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchLanePilotPreflight.mockResolvedValueOnce(null);

    render(<ImprovementLanesPanel projectId="proj-1" />);

    await screen.findAllByTestId('improvement-lane-row');
    expect(screen.queryByText('Preflight data unavailable')).toBeNull();
    expect(screen.queryByTestId('lane-pilot-preflight')).toBeNull();
  });

  test('shows preflight widget when preflight data is available', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchLanePilotPreflight.mockResolvedValueOnce({
      ready: true,
      pilot_mode: 'on',
      durable_verdict: 'MAINTAIN',
      checks: [
        { name: 'project_ready', met: true, blocking_value: null, fix_hint: null },
        { name: 'seed_approved', met: true, blocking_value: null, fix_hint: null },
      ],
    });

    render(<ImprovementLanesPanel projectId="proj-1" />);

    expect(await screen.findByTestId('lane-pilot-preflight')).toBeTruthy();
    expect(screen.getByText('Pilot ready to enable')).toBeTruthy();
    expect(screen.queryByText('Preflight data unavailable')).toBeNull();
  });

  test('expand toggle has correct aria-expanded state and calls fetchImprovementLaneDetail on open', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchImprovementLaneDetail.mockResolvedValueOnce({
      id: 'lane-1',
      title: null,
      goal: null,
      direction_id: 'd-123',
      version_number: null,
      status: 'ACTIVE',
      branch_name: 'lane/branch-a',
      base_branch: 'main',
      created_at: null,
      parked_at: null,
      last_resumed_at: null,
      retired_at: null,
      updated_at: null,
      last_checkpoint_sha: 'ccc333',
      last_checkpoint_phase: 'review',
      checkpoint_seq: 3,
      phase_requeues: 1,
      checkpoints: [],
      manifest: { phase_log: null, remaining: null, critical_files: null, failing_tests: null, risks: null, progress: null, recovery: null },
      workspace_state: null,
      workspace_vm_instance_id: null,
      pilot_arm: null,
      llm_input_tokens_total: null,
      llm_output_tokens_total: null,
      vm_cost_usd_total: 0,
      reserved_idle_cost_usd_total: 0,
      reservation_claimed_by_fix_count: 0,
      reservation_expired_before_fix_count: 0,
      reservation_force_expired_count: 0,
      current_task_id: null,
      current_task: null,
    });

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    const firstToggle = rows[0].querySelector('[data-testid="lane-expand-toggle"]') as HTMLElement;
    expect(firstToggle).toBeTruthy();
    expect(firstToggle.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(firstToggle);

    expect(firstToggle.getAttribute('aria-expanded')).toBe('true');
    await waitFor(() => expect(fetchImprovementLaneDetail).toHaveBeenCalledWith('proj-1', 'lane-1', expect.any(AbortSignal)));
  });

  test('LaneDetailPanel shows loading spinner while fetch is pending', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    let resolve!: (v: unknown) => void;
    fetchImprovementLaneDetail.mockReturnValueOnce(new Promise((r) => { resolve = r; }));

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(rows[0].querySelector('[data-testid="lane-expand-toggle"]') as HTMLElement);

    expect(await screen.findByText('Loading detail…')).toBeTruthy();
    resolve({
      id: 'lane-1', title: null, goal: null, direction_id: null, version_number: null,
      status: 'ACTIVE', branch_name: null, base_branch: null, created_at: null, parked_at: null,
      last_resumed_at: null, retired_at: null, updated_at: null, last_checkpoint_sha: null,
      last_checkpoint_phase: null, checkpoint_seq: 0, phase_requeues: 0, checkpoints: [],
      manifest: {}, workspace_state: null, workspace_vm_instance_id: null, pilot_arm: null,
      llm_input_tokens_total: null, llm_output_tokens_total: null, vm_cost_usd_total: 0,
      reserved_idle_cost_usd_total: 0, reservation_claimed_by_fix_count: 0,
      reservation_expired_before_fix_count: 0, reservation_force_expired_count: 0,
      current_task_id: null, current_task: null,
    });
    await waitFor(() => expect(screen.queryByText('Loading detail…')).toBeNull());
  });

  test('LaneDetailPanel shows error message when fetch fails', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchImprovementLaneDetail.mockRejectedValueOnce(new Error('Failed to fetch improvement lane detail: 403'));

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(rows[0].querySelector('[data-testid="lane-expand-toggle"]') as HTMLElement);

    expect(await screen.findByText('Failed to fetch improvement lane detail: 403')).toBeTruthy();
  });

  test('LaneDetailPanel renders checkpoint history table when checkpoints are present', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchImprovementLaneDetail.mockResolvedValueOnce({
      id: 'lane-1', title: null, goal: null, direction_id: null, version_number: null,
      status: 'ACTIVE', branch_name: null, base_branch: null, created_at: null, parked_at: null,
      last_resumed_at: null, retired_at: null, updated_at: null, last_checkpoint_sha: 'abc123',
      last_checkpoint_phase: 'fix', checkpoint_seq: 2, phase_requeues: 0,
      checkpoints: [
        {
          seq: 1, phase: 'fix', task_id: 'task-1', sha: 'abc123', parent_sha: null,
          created_at: '2026-06-11T10:00:00Z', summary: null,
          validated: { lint: true, tests: false }, wip_failing: null,
          tokens_in: 1000, tokens_out: 200, vm_cost_usd: 0.05, warm_resume: true,
        },
        {
          seq: 2, phase: 'review', task_id: 'task-2', sha: 'def456', parent_sha: 'abc123',
          created_at: '2026-06-11T11:00:00Z', summary: null,
          validated: { lint: null, tests: null }, wip_failing: null,
          tokens_in: null, tokens_out: null, vm_cost_usd: null, warm_resume: false,
        },
      ],
      manifest: {},
      workspace_state: null, workspace_vm_instance_id: null, pilot_arm: null,
      llm_input_tokens_total: null, llm_output_tokens_total: null, vm_cost_usd_total: 0,
      reserved_idle_cost_usd_total: 0, reservation_claimed_by_fix_count: 0,
      reservation_expired_before_fix_count: 0, reservation_force_expired_count: 0,
      current_task_id: 'task-2', current_task: null,
    });

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(rows[0].querySelector('[data-testid="lane-expand-toggle"]') as HTMLElement);

    // Shows checkpoint count
    expect(await screen.findByText('Checkpoint history (2)')).toBeTruthy();
    // Rows are reversed (latest first): seq 2 then seq 1
    const cells = await screen.findAllByText(/^(fix|review)$/);
    expect(cells.length).toBeGreaterThanOrEqual(2);
    // warm_resume rendering: seq1 = warm, seq2 = cold
    expect(await screen.findByText('warm')).toBeTruthy();
    expect(await screen.findByText('cold')).toBeTruthy();
  });

  test('LaneDetailPanel renders CurrentTaskSection with status badge when task is present', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchImprovementLaneDetail.mockResolvedValueOnce({
      id: 'lane-1', title: null, goal: null, direction_id: null, version_number: null,
      status: 'ACTIVE', branch_name: null, base_branch: null, created_at: null, parked_at: null,
      last_resumed_at: null, retired_at: null, updated_at: null, last_checkpoint_sha: null,
      last_checkpoint_phase: null, checkpoint_seq: 0, phase_requeues: 0, checkpoints: [],
      manifest: {},
      workspace_state: null, workspace_vm_instance_id: null, pilot_arm: null,
      llm_input_tokens_total: null, llm_output_tokens_total: null, vm_cost_usd_total: 0,
      reserved_idle_cost_usd_total: 0, reservation_claimed_by_fix_count: 0,
      reservation_expired_before_fix_count: 0, reservation_force_expired_count: 0,
      current_task_id: 'task-abc',
      current_task: { task_id: 'task-abc', status: 'IN_PROGRESS', title: 'Fix the bug', task_type: 'fix' },
    });

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(rows[0].querySelector('[data-testid="lane-expand-toggle"]') as HTMLElement);

    expect(await screen.findByText('IN_PROGRESS')).toBeTruthy();
    expect(screen.getByText('Fix the bug')).toBeTruthy();
    expect(screen.getByText('task-abc')).toBeTruthy();
  });

  test('LaneDetailPanel shows "No task currently running" when current_task is null', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchImprovementLaneDetail.mockResolvedValueOnce({
      id: 'lane-1', title: null, goal: null, direction_id: null, version_number: null,
      status: 'ACTIVE', branch_name: null, base_branch: null, created_at: null, parked_at: null,
      last_resumed_at: null, retired_at: null, updated_at: null, last_checkpoint_sha: null,
      last_checkpoint_phase: null, checkpoint_seq: 0, phase_requeues: 0, checkpoints: [],
      manifest: {},
      workspace_state: null, workspace_vm_instance_id: null, pilot_arm: null,
      llm_input_tokens_total: null, llm_output_tokens_total: null, vm_cost_usd_total: 0,
      reserved_idle_cost_usd_total: 0, reservation_claimed_by_fix_count: 0,
      reservation_expired_before_fix_count: 0, reservation_force_expired_count: 0,
      current_task_id: null, current_task: null,
    });

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(rows[0].querySelector('[data-testid="lane-expand-toggle"]') as HTMLElement);

    expect(await screen.findByText('No task currently running')).toBeTruthy();
  });

  test('LaneDetailPanel renders ManifestSection with phase_log array entries', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchImprovementLaneDetail.mockResolvedValueOnce({
      id: 'lane-1', title: null, goal: null, direction_id: null, version_number: null,
      status: 'ACTIVE', branch_name: null, base_branch: null, created_at: null, parked_at: null,
      last_resumed_at: null, retired_at: null, updated_at: null, last_checkpoint_sha: null,
      last_checkpoint_phase: null, checkpoint_seq: 0, phase_requeues: 0, checkpoints: [],
      manifest: {
        phase_log: ['fix: applied patch', 'review: lint passed'],
        critical_files: ['backend/app/main.py'],
        failing_tests: [],
        risks: null,
        progress: '50%',
        recovery: null,
        remaining: null,
      },
      workspace_state: null, workspace_vm_instance_id: null, pilot_arm: null,
      llm_input_tokens_total: null, llm_output_tokens_total: null, vm_cost_usd_total: 0,
      reserved_idle_cost_usd_total: 0, reservation_claimed_by_fix_count: 0,
      reservation_expired_before_fix_count: 0, reservation_force_expired_count: 0,
      current_task_id: null, current_task: null,
    });

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    fireEvent.click(rows[0].querySelector('[data-testid="lane-expand-toggle"]') as HTMLElement);

    expect(await screen.findByText('fix: applied patch')).toBeTruthy();
    expect(screen.getByText('review: lint passed')).toBeTruthy();
    expect(screen.getByText('backend/app/main.py')).toBeTruthy();
    expect(screen.getByText('50%')).toBeTruthy();
  });

  test('clicking expand toggle again collapses the detail panel', async () => {
    fetchImprovementLanes.mockResolvedValueOnce(LANE_RESPONSE);
    fetchImprovementLaneDetail.mockResolvedValueOnce({
      id: 'lane-1', title: null, goal: null, direction_id: null, version_number: null,
      status: 'ACTIVE', branch_name: null, base_branch: null, created_at: null, parked_at: null,
      last_resumed_at: null, retired_at: null, updated_at: null, last_checkpoint_sha: null,
      last_checkpoint_phase: null, checkpoint_seq: 0, phase_requeues: 0, checkpoints: [],
      manifest: {},
      workspace_state: null, workspace_vm_instance_id: null, pilot_arm: null,
      llm_input_tokens_total: null, llm_output_tokens_total: null, vm_cost_usd_total: 0,
      reserved_idle_cost_usd_total: 0, reservation_claimed_by_fix_count: 0,
      reservation_expired_before_fix_count: 0, reservation_force_expired_count: 0,
      current_task_id: null, current_task: null,
    });

    render(<ImprovementLanesPanel projectId="proj-1" />);

    const rows = await screen.findAllByTestId('improvement-lane-row');
    const toggle = rows[0].querySelector('[data-testid="lane-expand-toggle"]') as HTMLElement;

    fireEvent.click(toggle);
    expect(await screen.findByText('No task currently running')).toBeTruthy();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');

    fireEvent.click(toggle);
    await waitFor(() => expect(screen.queryByText('No task currently running')).toBeNull());
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
  });
});
