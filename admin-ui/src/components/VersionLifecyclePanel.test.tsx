import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { VersionLifecyclePanel } from './VersionLifecyclePanel';
import * as api from '../api';
import type { VersionLifecycleResponse } from '../api';

vi.mock('../api');

// Mock window.confirm for cut version tests
const mockConfirm = vi.spyOn(window, 'confirm');

function makeCurrentVersion(overrides: Partial<api.CurrentVersion> = {}): api.CurrentVersion {
  return {
    version_number: 1,
    phase: 'build',
    phase_changed_at: '2026-01-01T00:00:00Z',
    phase_reasoning: null,
    goal: 'Initial version',
    goal_updated_at: '2026-01-01T00:00:00Z',
    goal_editor_info: 'operator',
    opened_at: '2026-01-01T00:00:00Z',
    qualification_attempts: 0,
    merged_task_count: 0,
    qualification_state: null,
    last_qualification: null,
    latest_audit: null,
    ...overrides,
  };
}

function makeVersionLifecycleResponse(overrides: Partial<VersionLifecycleResponse> = {}): VersionLifecycleResponse {
  return {
    current_version: makeCurrentVersion(),
    recent_versions: [],
    promotion_enabled: false,
    promotion_readiness: null,
    ...overrides,
  };
}

describe('VersionLifecyclePanel', () => {
  const mockRefresh = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mockRefresh.mockClear();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  describe('phase updates reflect immediately', () => {
    it('shows new phase immediately after successful phase change without waiting for refresh', async () => {
      const setVersionPhaseMock = vi.fn().mockResolvedValue({
        version_number: 1,
        phase: 'stabilize',
        phase_changed_at: '2026-01-02T00:00:00Z',
        phase_reasoning: 'operator advance',
      });
      vi.mocked(api.setVersionPhase).mockImplementation(setVersionPhaseMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ phase: 'build' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const stabilizeButton = screen.getByRole('button', { name: /stabilize/ });
      fireEvent.click(stabilizeButton);

      await waitFor(() => {
        expect(setVersionPhaseMock).toHaveBeenCalledWith('proj-1', 'stabilize', 'operator advance');
      });

      await waitFor(() => {
        const phaseBadge = screen.getByText('stabilize');
        expect(phaseBadge).toBeInTheDocument();
      });

      expect(mockRefresh).toHaveBeenCalled();
    });

    it('reverts phase change when mutation fails', async () => {
      const setVersionPhaseMock = vi.fn().mockRejectedValue(new Error('Network error'));
      vi.mocked(api.setVersionPhase).mockImplementation(setVersionPhaseMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ phase: 'build' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const stabilizeButton = screen.getByRole('button', { name: /stabilize/ });
      fireEvent.click(stabilizeButton);

      await waitFor(() => {
        expect(screen.getByText('Request failed')).toBeInTheDocument();
      });

      const phaseBadge = screen.getByText('build');
      expect(phaseBadge).toBeInTheDocument();
    });

    it('clears optimistic phase update when data refreshes', async () => {
      const setVersionPhaseMock = vi.fn().mockResolvedValue({
        version_number: 1,
        phase: 'stabilize',
        phase_changed_at: '2026-01-02T00:00:00Z',
        phase_reasoning: 'operator advance',
      });
      vi.mocked(api.setVersionPhase).mockImplementation(setVersionPhaseMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      const { rerender } = render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ phase: 'build' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const stabilizeButton = screen.getByRole('button', { name: /stabilize/ });
      fireEvent.click(stabilizeButton);

      await waitFor(() => {
        expect(screen.getByText('stabilize')).toBeInTheDocument();
      });

      rerender(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ phase: 'stabilize' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      await waitFor(() => {
        expect(screen.queryByText('Request failed')).not.toBeInTheDocument();
      });
    });
  });

  describe('goal updates reflect immediately', () => {
    it('shows new goal immediately after successful goal update without waiting for refresh', async () => {
      const setVersionGoalMock = vi.fn().mockResolvedValue({
        version_number: 1,
        goal: 'New goal text',
        goal_updated_at: '2026-01-02T00:00:00Z',
        goal_editor_info: 'operator',
      });
      vi.mocked(api.setVersionGoal).mockImplementation(setVersionGoalMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ goal: 'Initial goal' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const editButton = screen.getByRole('button', { name: /Edit/ });
      fireEvent.click(editButton);

      const textarea = screen.getByPlaceholderText('Enter version goal...');
      fireEvent.change(textarea, { target: { value: 'New goal text' } });

      const saveButton = screen.getByRole('button', { name: /Save/ });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(setVersionGoalMock).toHaveBeenCalledWith('proj-1', 'New goal text');
      });

      await waitFor(() => {
        expect(screen.getByText('New goal text')).toBeInTheDocument();
      });

      expect(mockRefresh).toHaveBeenCalled();
    });

    it('reverts goal change when mutation fails', async () => {
      const setVersionGoalMock = vi.fn().mockRejectedValue(new Error('Network error'));
      vi.mocked(api.setVersionGoal).mockImplementation(setVersionGoalMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ goal: 'Initial goal' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const editButton = screen.getByRole('button', { name: /Edit/ });
      fireEvent.click(editButton);

      const textarea = screen.getByPlaceholderText('Enter version goal...');
      fireEvent.change(textarea, { target: { value: 'New goal text' } });

      const saveButton = screen.getByRole('button', { name: /Save/ });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText('Request failed')).toBeInTheDocument();
      });

      const goalText = screen.getByText('Initial goal');
      expect(goalText).toBeInTheDocument();
    });

    it('clears optimistic goal update when data refreshes', async () => {
      const setVersionGoalMock = vi.fn().mockResolvedValue({
        version_number: 1,
        goal: 'New goal text',
        goal_updated_at: '2026-01-02T00:00:00Z',
        goal_editor_info: 'operator',
      });
      vi.mocked(api.setVersionGoal).mockImplementation(setVersionGoalMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      const { rerender } = render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ goal: 'Initial goal' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const editButton = screen.getByRole('button', { name: /Edit/ });
      fireEvent.click(editButton);

      const textarea = screen.getByPlaceholderText('Enter version goal...');
      fireEvent.change(textarea, { target: { value: 'New goal text' } });

      const saveButton = screen.getByRole('button', { name: /Save/ });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText('New goal text')).toBeInTheDocument();
      });

      rerender(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ goal: 'New goal text' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      await waitFor(() => {
        expect(screen.queryByText('Request failed')).not.toBeInTheDocument();
      });
    });
  });

  describe('promotion enabled toggle', () => {
    it('confirms before enabling live promotion and updates the badge immediately', async () => {
      const patchAdminSettingsMock = vi.fn().mockResolvedValue({});
      vi.mocked(api.patchAdminSettings).mockImplementation(patchAdminSettingsMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ promotion_enabled: false })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          userRole="admin"
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Enable/ }));

      await waitFor(() => {
        expect(window.confirm).toHaveBeenCalledWith('Enable live promotion for ALL PROJECTS? Passing qualify runs will push to main fleet-wide.');
      });
      await waitFor(() => {
        expect(patchAdminSettingsMock).toHaveBeenCalledWith({ promotion_enabled: true });
      });
      expect(screen.getByText('LIVE')).toBeInTheDocument();
    });

    it('reverts the badge when patching promotion settings fails', async () => {
      const patchAdminSettingsMock = vi.fn().mockRejectedValue(new Error('Network error'));
      vi.mocked(api.patchAdminSettings).mockImplementation(patchAdminSettingsMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ promotion_enabled: false })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          userRole="admin"
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Enable/ }));

      await waitFor(() => {
        expect(screen.getByText('Request failed')).toBeInTheDocument();
      });
      expect(screen.getByText('DRY-RUN')).toBeInTheDocument();
    });
  });

  describe('per-project promotion control', () => {
    it('arms live promotion for the project and reports the override source', async () => {
      const setProjectPromotionEnabledMock = vi.fn().mockResolvedValue({
        project_id: 'proj-1', override: true, effective: true, source: 'project', cleared: false,
      });
      vi.mocked(api.setProjectPromotionEnabled).mockImplementation(setProjectPromotionEnabledMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            promotion_enabled: false,
            promotion_enabled_effective: false,
            promotion_enabled_override: null,
            promotion_enabled_source: 'fleet',
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          userRole="admin"
        />
      );

      expect(screen.getByText(/fleet default/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: /^Arm$/ }));

      await waitFor(() => {
        expect(setProjectPromotionEnabledMock).toHaveBeenCalledWith('proj-1', true);
      });
    });

    it('offers a clear control that falls back to the fleet default when an override is set', async () => {
      vi.mocked(api.setProjectPromotionEnabled).mockResolvedValue({
        project_id: 'proj-1', override: null, effective: false, source: 'fleet', cleared: true,
      });
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            promotion_enabled: false,
            promotion_enabled_effective: true,
            promotion_enabled_override: true,
            promotion_enabled_source: 'project',
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          userRole="admin"
        />
      );

      expect(screen.getByText(/project override/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: /Use fleet default/ }));

      await waitFor(() => {
        expect(api.setProjectPromotionEnabled).toHaveBeenCalledWith('proj-1', null);
      });
    });
  });

  describe('cut/abandon version', () => {
    it('renders cut/abandon button when there is an open version', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ version_number: 1 }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByRole('button', { name: /Cut\/Abandon/ })).toBeInTheDocument();
    });

    it('shows confirm guard and calls API on cut version', async () => {
      const cutVersionMock = vi.fn().mockResolvedValue({
        abandoned_version: { version_number: 1, status: 'abandoned' },
        new_version: { version_number: 2, phase: 'build', goal: null, opened_at: '2026-01-02T00:00:00Z' },
      });
      vi.mocked(api.cutVersion).mockImplementation(cutVersionMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      mockConfirm.mockReturnValue(true);

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ version_number: 1 }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const cutButton = screen.getByRole('button', { name: /Cut\/Abandon/ });
      fireEvent.click(cutButton);

      // Should show the input and buttons
      const reasonInput = screen.getByPlaceholderText('Optional reason for cut...');
      expect(reasonInput).toBeInTheDocument();

      const cutButtonInForm = screen.getByRole('button', { name: /^Cut$/ });
      fireEvent.click(cutButtonInForm);

      await waitFor(() => {
        expect(window.confirm).toHaveBeenCalledWith('Abandon v1 and open a fresh version?');
      });

      await waitFor(() => {
        expect(cutVersionMock).toHaveBeenCalledWith('proj-1', undefined, expect.any(String));
      });

      expect(mockRefresh).toHaveBeenCalled();
    });

    it('calls cutVersion with reason when provided', async () => {
      const cutVersionMock = vi.fn().mockResolvedValue({
        abandoned_version: { version_number: 1, status: 'abandoned' },
        new_version: { version_number: 2, phase: 'build', goal: null, opened_at: '2026-01-02T00:00:00Z' },
      });
      vi.mocked(api.cutVersion).mockImplementation(cutVersionMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      mockConfirm.mockReturnValue(true);

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ version_number: 1 }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));

      const reasonInput = screen.getByPlaceholderText('Optional reason for cut...') as HTMLInputElement;
      fireEvent.change(reasonInput, { target: { value: 'stalled in build' } });

      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));

      await waitFor(() => {
        expect(cutVersionMock).toHaveBeenCalledWith('proj-1', 'stalled in build', expect.any(String));
      });
    });

    it('cancels cut version action when confirm is rejected', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      mockConfirm.mockReturnValue(false);

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ version_number: 1 }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));
      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));

      await waitFor(() => {
        expect(window.confirm).toHaveBeenCalledWith('Abandon v1 and open a fresh version?');
      });

      expect(vi.mocked(api.cutVersion)).not.toHaveBeenCalled();
    });

    it('shows action message on successful cut version', async () => {
      const cutVersionMock = vi.fn().mockResolvedValue({
        abandoned_version: { version_number: 5, status: 'abandoned' },
        new_version: { version_number: 6, phase: 'build', goal: null, opened_at: '2026-01-02T00:00:00Z' },
      });
      vi.mocked(api.cutVersion).mockImplementation(cutVersionMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      mockConfirm.mockReturnValue(true);

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ version_number: 5 }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));
      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));

      await waitFor(() => {
        expect(screen.getByText('Cut v5 → opened v6')).toBeInTheDocument();
      });
    });

    it('reuses the same idempotency key when a failed cut is retried', async () => {
      const cutVersionMock = vi
        .fn()
        .mockRejectedValueOnce(new Error('network blip'))
        .mockResolvedValueOnce({
          abandoned_version: { version_number: 1, status: 'abandoned' },
          new_version: { version_number: 2, phase: 'build', goal: null, opened_at: '2026-01-02T00:00:00Z' },
        });
      vi.mocked(api.cutVersion).mockImplementation(cutVersionMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      mockConfirm.mockReturnValue(true);

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ version_number: 1 }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));
      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));
      await waitFor(() => expect(cutVersionMock).toHaveBeenCalledTimes(1));

      // Retry after the failure (the cut UI stays open on error).
      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));
      await waitFor(() => expect(cutVersionMock).toHaveBeenCalledTimes(2));

      // Both calls carried the SAME generated key (a lost-response retry must
      // not advance the version a second time on the backend).
      const firstKey = cutVersionMock.mock.calls[0][2];
      const secondKey = cutVersionMock.mock.calls[1][2];
      expect(typeof firstKey).toBe('string');
      expect(firstKey).toBeTruthy();
      expect(secondKey).toBe(firstKey);
    });
  });

  describe('history rendering', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('shows operator provenance for abandoned versions', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: null,
            recent_versions: [
              {
                version_number: 1,
                status: 'abandoned',
                opened_at: '2026-01-01T00:00:00Z',
                promoted_at: null,
                qualification_attempts: 1,
                last_qualification: null,
                abandoned_at: '2026-01-02T03:04:05Z',
                abandoned_by: 'alice@example.com',
                abandon_reason: 'candidate can never pass',
              },
            ],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByText('abandoned')).toBeInTheDocument();
      expect(screen.getByText(/cut by alice@example.com: candidate can never pass/)).toBeInTheDocument();
    });

    it('does not add provenance text to non-abandoned rows', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: null,
            recent_versions: [
              {
                version_number: 2,
                status: 'promoted',
                opened_at: '2026-01-01T00:00:00Z',
                promoted_at: '2026-01-03T00:00:00Z',
                qualification_attempts: 2,
                last_qualification: null,
              },
            ],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByText('promoted')).toBeInTheDocument();
      expect(screen.queryByText(/cut by /)).not.toBeInTheDocument();
    });
  });

  describe('staged directions summary', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('renders staged directions with target version and age from live data', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            staged_directions: [
              { id: 'd-1', text: 'Rework packer budget', priority: 'high', staged_for_version: 6, staged_at: '2026-05-01T00:00:00Z', staged_age_days: 35 },
            ],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByTestId('staged-directions')).toBeInTheDocument();
      expect(screen.getByText('Staged for next version (1)')).toBeInTheDocument();
      expect(screen.getByText('Rework packer budget')).toBeInTheDocument();
      expect(screen.getByText('→v6')).toBeInTheDocument();
      expect(screen.getByText('35d staged')).toBeInTheDocument();
    });

    it('omits the staged section when no directions are staged', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ staged_directions: [] })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.queryByTestId('staged-directions')).not.toBeInTheDocument();
    });
  });

  describe('cut consequences disclosure', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('discloses activating staged directions and in-flight count before confirming', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ version_number: 6, in_flight_task_count: 3 }),
            staged_directions: [
              { id: 'd-1', text: 'Rework packer budget', priority: 'high', staged_for_version: 6, staged_at: '2026-05-01T00:00:00Z', staged_age_days: 35 },
              { id: 'd-2', text: 'Future-only work', priority: 'low', staged_for_version: 9, staged_at: '2026-05-01T00:00:00Z', staged_age_days: 1 },
            ],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      // Expand the cut affordance.
      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));

      const consequences = screen.getByTestId('cut-consequences');
      expect(consequences).toBeInTheDocument();
      // Direction targeting v6 (<= 6) activates; v9 (> 6) does not.
      expect(within(consequences).getByText('Rework packer budget')).toBeInTheDocument();
      expect(within(consequences).queryByText('Future-only work')).not.toBeInTheDocument();
      expect(consequences).toHaveTextContent('Start 1 staged direction immediately');
      expect(consequences).toHaveTextContent('3 in-flight tasks will NOT be stopped');
    });

    it('shows a no-effect affordance when nothing is staged or in flight', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ version_number: 2, in_flight_task_count: 0 }),
            staged_directions: [],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));

      const consequences = screen.getByTestId('cut-consequences');
      expect(consequences).toHaveTextContent('No staged directions will activate.');
      expect(consequences).toHaveTextContent('No tasks are currently in flight.');
    });

    it('folds consequences into the confirm message', async () => {
      const cutVersionMock = vi.fn().mockResolvedValue({
        abandoned_version: { version_number: 6, status: 'abandoned' },
        new_version: { version_number: 7, phase: 'build', goal: null, opened_at: '2026-01-02T00:00:00Z' },
      });
      vi.mocked(api.cutVersion).mockImplementation(cutVersionMock);
      mockConfirm.mockReturnValue(true);

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ version_number: 6, in_flight_task_count: 2 }),
            staged_directions: [
              { id: 'd-1', text: 'Rework packer budget', priority: 'high', staged_for_version: 6, staged_at: '2026-05-01T00:00:00Z', staged_age_days: 35 },
            ],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));
      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));

      await waitFor(() => {
        expect(window.confirm).toHaveBeenCalledWith(
          'Abandon v6 and open a fresh version?\n\nThis will: 1 staged direction will start now; 2 in-flight tasks will keep running against the new version.'
        );
      });
    });

    it('surfaces promotion-in-flight context before confirming a cut', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      vi.mocked(api.cutVersion).mockRejectedValue(
        new Error('a promotion PR is open and auto-merging — wait for it to land or cancel it before cutting (PR #7357 https://example/pr/7357)'),
      );

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              version_number: 6,
              qualification_state: {
                state: 'promotion_in_flight',
                summary: 'Audit PASSED; a promotion PR is open and auto-merging.',
                promotion_pr_number: 7357,
                promotion_pr_url: 'https://example/pr/7357',
              },
            }),
            staged_directions: [],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));

      const consequences = screen.getByTestId('cut-consequences');
      expect(consequences).toHaveTextContent('Promotion PR #7357 is open and auto-merging');

      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));

      await waitFor(() => {
        expect(window.confirm).toHaveBeenCalledWith(
          expect.stringContaining('promotion PR #7357 is open and auto-merging')
        );
      });
      await waitFor(() => {
        expect(screen.getByText(/wait for it to land or cancel it before cutting/)).toBeInTheDocument();
      });
    });
  });

  describe('audit findings provenance + staleness', () => {
    function makeQualification(overrides: Partial<api.VersionQualification> = {}): api.VersionQualification {
      return {
        status: 'not_promoted',
        confidence: 0.89,
        threshold: 0.9,
        sub_scores: null,
        ci_resolution: null,
        blocker: null,
        finalized_at: '2026-01-01T00:00:00Z',
        ...overrides,
      };
    }

    function makeLatestAudit(overrides: Partial<api.LatestAudit> = {}): api.LatestAudit {
      return {
        audit_task_id: 'task-1',
        task_status: 'completed',
        candidate_sha: 'abcdef1234567890',
        created_at: '2026-01-01T00:00:00Z',
        report: { findings: [{ severity: 'medium', summary: 'unverified finding' }] },
        ...overrides,
      };
    }

    it('renders the audit candidate SHA and run-age provenance', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              latest_audit: makeLatestAudit({ candidate_sha: 'abcdef1234567890' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText('@abcdef1')).toBeInTheDocument();
      expect(screen.getByText(/^ran /)).toBeInTheDocument();
    });

    it('shows a stale badge when the audit commit differs from the evaluated candidate', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'fed9876543210000' }),
              latest_audit: makeLatestAudit({ candidate_sha: 'abcdef1234567890' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText(/findings from an older commit/)).toBeInTheDocument();
    });

    it('does not show a stale badge when the audit commit matches the evaluated candidate', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'abcdef1234567890' }),
              latest_audit: makeLatestAudit({ candidate_sha: 'abcdef1234567890' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByText(/findings from an older commit/)).not.toBeInTheDocument();
    });

    it('renders an "as of" freshness label beside the qualification summary', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ finalized_at: '2026-01-01T00:00:00Z' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText(/^as of /)).toBeInTheDocument();
    });
  });
});
