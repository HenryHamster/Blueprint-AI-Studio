import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { VersionLifecyclePanel } from './VersionLifecyclePanel';
import * as api from '../api';
import type { VersionLifecycleResponse } from '../api';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn().mockResolvedValue('test-token'),
}));
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
    active_candidate_sha: null,
    latest_audit: null,
    audit_evidence_state: 'none',
    stale_audit: null,
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

function makePromotionInFlightState(
  overrides: Partial<api.QualificationState> = {},
): api.QualificationState {
  return {
    state: 'promotion_in_flight',
    summary: 'Audit PASSED; a promotion PR is open and auto-merge is armed.',
    promotion_pr_number: 4242,
    promotion_pr_url: 'https://example/pr/4242',
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
              qualification_state: makePromotionInFlightState({
                promotion_pr_number: 7357,
                promotion_pr_url: 'https://example/pr/7357',
                promotion_age_seconds: 7500,
                promotion_timeout_seconds: 14400,
              }),
            }),
            staged_directions: [],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));

      expect(screen.getByTestId('promotion-in-flight-freshness')).toHaveTextContent(
        'Promotion PR #7357 open & auto-merging (in flight ~2h 5m (auto-timeout cap 4h))'
      );
      const consequences = screen.getByTestId('cut-consequences');
      expect(consequences).toHaveTextContent(
        'Promotion PR #7357 is open and auto-merging (in flight ~2h 5m (auto-timeout cap 4h))'
      );

      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));

      await waitFor(() => {
        expect(window.confirm).toHaveBeenCalledWith(
          expect.stringContaining('promotion PR #7357 is open and auto-merging (in flight ~2h 5m (auto-timeout cap 4h))')
        );
      });
      await waitFor(() => {
        expect(screen.getByText(/wait for it to land or cancel it before cutting/)).toBeInTheDocument();
      });
    });
  });

  describe('audit evidence rendering', () => {
    it('renders the current audit findings with provenance for the active candidate', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              audit_evidence_state: 'current',
              latest_audit: makeLatestAudit({ candidate_sha: 'abcdef1234567890' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText('Audit Findings')).toBeInTheDocument();
      expect(screen.getByText('unverified finding')).toBeInTheDocument();
      expect(screen.getByText('@abcdef1')).toBeInTheDocument();
      expect(screen.getByText(/^ran /)).toBeInTheDocument();
    });

    it('renders stale audit provenance as historical context and contrasts the active candidate with the audited one', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'fed9876543210000' }),
              active_candidate_sha: 'fed9876543210000',
              audit_evidence_state: 'stale',
              latest_audit: null,
              stale_audit: makeLatestAudit({ candidate_sha: 'abcdef1234567890' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText('Historical Audit Context')).toBeInTheDocument();
      expect(screen.getByText(/no current audit for this candidate/i)).toBeInTheDocument();
      expect(screen.getByText(/^ran /)).toBeInTheDocument();
      expect(screen.queryByText('unverified finding')).not.toBeInTheDocument();
      expect(screen.queryByText('Audit Findings')).not.toBeInTheDocument();
      // The historical-context block explicitly contrasts the active candidate
      // SHA (awaiting a fresh audit) with the older audited candidate SHA.
      const ctx = screen.getByTestId('historical-audit-context');
      expect(within(ctx).getByText('@fed9876')).toBeInTheDocument();
      expect(within(ctx).getByText('@abcdef1')).toBeInTheDocument();
    });

    it('shows the candidate SHA currently under evaluation on the open version', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              active_candidate_sha: 'abcdef1234567890',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByTestId('active-candidate-sha')).toHaveTextContent('@abcdef1');
    });

    it('does not display inherited findings when the current candidate has no audit evidence', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'abcdef1234567890' }),
              audit_evidence_state: 'none',
              latest_audit: null,
              stale_audit: null,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByText('Audit Findings')).not.toBeInTheDocument();
      expect(screen.queryByText(/no current audit for this candidate/i)).not.toBeInTheDocument();
      expect(screen.queryByText('unverified finding')).not.toBeInTheDocument();
    });

    it('preserves the crashed-audit affordance for a terminally-failed candidate audit', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              audit_evidence_state: 'current',
              latest_audit: makeLatestAudit({ report: null, task_status: 'FAILED' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText(/audit crashed — no report/)).toBeInTheDocument();
    });

    it('shows a cancelled no-report audit as crashed', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              audit_evidence_state: 'current',
              latest_audit: makeLatestAudit({ report: null, task_status: 'CANCELLED' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText(/audit crashed — no report/)).toBeInTheDocument();
    });

    it('does not contradict itself: a crashed audit child with a pending verdict shows the reconciled crash state, not "in flight / wait for the verdict"', () => {
      // Backend reconciles a pending verdict whose audit child crashed
      // (FAILED/CANCELLED, no report) into the audit_crashed qualification_state.
      // The panel must render that reconciled top badge/summary in agreement with
      // the audit-findings section's 'audit crashed — no report' cue — never the
      // old 'qualifying / wait for the verdict' contradiction.
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ status: 'pending' }),
              qualification_state: {
                state: 'audit_crashed',
                summary:
                  'The qualification audit child CRASHED (terminal-failed with no report) while the verdict is still pending. The audit is DEAD — do NOT treat this as audit in flight.',
              },
              audit_evidence_state: 'current',
              latest_audit: makeLatestAudit({ report: null, task_status: 'FAILED' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      // The audit-findings section reports the crash...
      expect(screen.getByText(/audit crashed — no report/)).toBeInTheDocument();
      // ...and the top qualification surface AGREES — no 'in flight / wait' copy.
      expect(screen.queryByText(/wait for the verdict/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/audit is in flight/i)).not.toBeInTheDocument();
      // The reconciled crash badge renders (red audit_crashed style).
      expect(screen.getByText('audit crashed')).toBeInTheDocument();
    });

    it.each(['IN_PROGRESS', 'QUEUED', 'PROVISIONING', 'EVALUATING'])(
      'shows an in-flight no-report audit (%s) as running, not crashed',
      (status) => {
        render(
          <VersionLifecyclePanel
            data={makeVersionLifecycleResponse({
              current_version: makeCurrentVersion({
                audit_evidence_state: 'current',
                latest_audit: makeLatestAudit({ report: null, task_status: status }),
              }),
            })}
            loading={false}
            projectId="proj-1"
            onRefresh={mockRefresh}
          />
        );
        expect(screen.getByText(/audit running — no report yet/)).toBeInTheDocument();
        expect(screen.queryByText(/audit crashed/)).not.toBeInTheDocument();
      }
    );

    it('shows a blocked no-report audit as blocked, distinct from crashed', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              audit_evidence_state: 'current',
              latest_audit: makeLatestAudit({ report: null, task_status: 'BLOCKED' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText(/audit blocked — waiting on a precondition/)).toBeInTheDocument();
      expect(screen.queryByText(/audit crashed/)).not.toBeInTheDocument();
    });

    it('shows a completed-but-empty no-report audit as a distinct anomaly, not a crash', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              audit_evidence_state: 'current',
              latest_audit: makeLatestAudit({ report: null, task_status: 'COMPLETED' }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText(/audit completed without a report/)).toBeInTheDocument();
      expect(screen.queryByText(/audit crashed/)).not.toBeInTheDocument();
    });

    it('preserves the no-findings affordance only for the current candidate audit', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              audit_evidence_state: 'current',
              latest_audit: makeLatestAudit({ report: { findings: [] } }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText(/audit: no findings/)).toBeInTheDocument();
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

    it('renders durable qualification evidence on the open version', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                migration_safety: 'block',
                migration_safety_detail: 'open version needs a schema migration',
                migration_apply_safety: 'warn',
                migration_apply_safety_detail: 'apply path still diverges',
                audit_findings: [{ severity: 'high', summary: 'unverified audit finding' }],
                excluded_audit_findings: [{ severity: 'low', summary: 'stale noise' }],
                test_erosion_detail: { deleted_test_files: 1 },
                smoketest_detail: { passed: false, suite: 'release' },
                promotion_pr_url: 'https://example.com/pr/open',
                promotion_conflict: true,
                promotion_error_detail: 'merge conflict',
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      expect(within(evidence).getByText(/1 audit finding/)).toBeInTheDocument();
      expect(within(evidence).getByText(/1 excluded finding/)).toBeInTheDocument();
      expect(within(evidence).getByText(/migration block/)).toBeInTheDocument();
      expect(within(evidence).getByText(/apply warn/)).toBeInTheDocument();
      expect(within(evidence).getByText(/promotion conflict/)).toBeInTheDocument();
      expect(within(evidence).getByTitle('open version needs a schema migration')).toBeInTheDocument();
      expect(within(evidence).getByTitle('apply path still diverges')).toBeInTheDocument();
    });

    it('renders durable qualification evidence in verdict history', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [
          {
            id: 'v1',
            release_version_id: 'rv1',
            candidate_sha: 'deadbeef1234',
            confidence: 0.95,
            sub_scores: null,
            blocker: null,
            audit_missing_reason: null,
            audit_findings: [{ severity: 'high', summary: 'durable finding' }],
            excluded_audit_findings: [{ severity: 'low', summary: 'excluded noise' }],
            migration_safety: 'pass',
            migration_safety_detail: 'schema migration verified',
            migration_apply_safety: 'pass',
            migration_apply_safety_detail: 'apply path verified',
            test_erosion_detail: { deleted_test_files: 0 },
            smoketest_detail: { passed: true },
            audit_reconciliation: null,
            promotion_pr_url: 'https://example.com/pr/123',
            promotion_conflict: false,
            promotion_error_detail: null,
            would_promote: true,
            promotion_enabled: true,
            terminal_status: 'promoted',
            threshold: 0.9,
            started_at: null,
            finalized_at: '2026-01-02T00:00:00Z',
            created_at: '2026-01-02T00:00:00Z',
            post_promotion_outcome: 'deploy_failed',
          },
        ],
        total: 1,
      });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse()}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const history = await screen.findByTestId('verdict-history');
      const evidence = within(history).getByTestId('durable-qualification-evidence');
      expect(within(evidence).getByText(/1 audit finding/)).toBeInTheDocument();
      expect(within(evidence).getByText(/migration pass/)).toBeInTheDocument();
      expect(within(evidence).getByText(/apply pass/)).toBeInTheDocument();
      expect(within(evidence).getByText(/promotion PR/)).toBeInTheDocument();
      expect(within(evidence).getByTitle('schema migration verified')).toBeInTheDocument();
      expect(within(evidence).getByTitle('apply path verified')).toBeInTheDocument();
    });
  });

  describe('qualification verdict staleness', () => {
    it('flags the verdict as stale and shows the verdict candidate SHA when tasks merged after it', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'abcdef1234567890' }),
              merged_since_verdict: 3,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(
        screen.getByText(/3 tasks merged since this verdict — re-qualify for current state/)
      ).toBeInTheDocument();
      expect(screen.getByText('@abcdef1')).toBeInTheDocument();
    });

    it('singularizes the staleness flag for a single merge', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'abcdef1234567890' }),
              merged_since_verdict: 1,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(
        screen.getByText(/1 task merged since this verdict — re-qualify for current state/)
      ).toBeInTheDocument();
    });

    it('renders no staleness flag when zero tasks merged after the verdict', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'abcdef1234567890' }),
              merged_since_verdict: 0,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByText(/merged since this verdict/)).not.toBeInTheDocument();
    });

    it('renders no staleness flag when merged_since_verdict is null/absent', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'abcdef1234567890' }),
              merged_since_verdict: null,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByText(/merged since this verdict/)).not.toBeInTheDocument();
    });

    it('renders no verdict SHA or staleness flag gracefully when there is no candidate_sha', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: null }),
              merged_since_verdict: 0,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByText(/^@/)).not.toBeInTheDocument();
      expect(screen.queryByText(/merged since this verdict/)).not.toBeInTheDocument();
    });
  });

  describe('promotion-in-flight locks qualify + phase controls', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    const promotionInFlightState = makePromotionInFlightState();

    it('disables Run Qualify and the phase flip while a promotion is in flight', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: promotionInFlightState,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByRole('button', { name: /Run Qualify/ })).toBeDisabled();
      // In stabilize phase, the phase-flip button targets build.
      expect(screen.getByRole('button', { name: /build/ })).toBeDisabled();
      // The note makes the locked state legible.
      expect(screen.getByTestId('promotion-in-flight-note')).toHaveTextContent('#4242');
    });

    it('degrades cleanly to the PR-only lock copy when freshness timing is absent', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: promotionInFlightState,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByTestId('promotion-in-flight-freshness')).not.toBeInTheDocument();
      expect(screen.getByTestId('promotion-in-flight-note')).toHaveTextContent(
        'Promotion PR #4242 open & auto-merging — qualify & phase locked until it lands or is canceled'
      );
      expect(screen.getByRole('button', { name: /Run Qualify/ })).toHaveAttribute(
        'title',
        'A promotion PR is open and auto-merging — wait for it to land or cancel it before re-qualifying'
      );
    });

    it('surfaces a nearing-timeout warning consistently across the status and lock copy', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: makePromotionInFlightState({
                promotion_age_seconds: 12600,
                promotion_timeout_seconds: 14400,
                promotion_nearing_timeout: true,
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByTestId('promotion-in-flight-freshness')).toHaveTextContent(
        'Promotion PR #4242 open & auto-merging (in flight ~3h 30m (auto-timeout cap 4h), nearing auto-timeout)'
      );
      expect(screen.getByTestId('promotion-in-flight-note')).toHaveTextContent(
        'Promotion PR #4242 open & auto-merging (in flight ~3h 30m (auto-timeout cap 4h), nearing auto-timeout) — qualify & phase locked until it lands or is canceled'
      );
      expect(screen.getByRole('button', { name: /Run Qualify/ })).toHaveAttribute(
        'title',
        'A promotion PR is open and auto-merging (in flight ~3h 30m (auto-timeout cap 4h), nearing auto-timeout) — wait for it to land or cancel it before re-qualifying'
      );
    });

    it('surfaces a past-timeout warning consistently across the status and lock copy', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: makePromotionInFlightState({
                promotion_age_seconds: 14700,
                promotion_timeout_seconds: 14400,
                promotion_past_timeout: true,
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByTestId('promotion-in-flight-freshness')).toHaveTextContent(
        'Promotion PR #4242 open & auto-merging (in flight ~4h 5m (auto-timeout cap 4h), past auto-timeout)'
      );
      expect(screen.getByTestId('promotion-in-flight-note')).toHaveTextContent(
        'Promotion PR #4242 open & auto-merging (in flight ~4h 5m (auto-timeout cap 4h), past auto-timeout) — qualify & phase locked until it lands or is canceled'
      );
    });

    it('leaves qualify + phase controls actionable when no promotion is in flight', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: { state: 'qualifying', summary: 'Audit in flight.' },
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByRole('button', { name: /Run Qualify/ })).toBeEnabled();
      expect(screen.getByRole('button', { name: /build/ })).toBeEnabled();
      expect(screen.queryByTestId('promotion-in-flight-note')).not.toBeInTheDocument();
    });
  });

  describe('promotion readiness regime labeling', () => {
    function makeReadiness(overrides: Partial<api.PromotionReadiness> = {}): api.PromotionReadiness {
      return {
        ready: true,
        reason: 'qualifier self-approval streak clears thresholds',
        verdicts_evaluated: 6,
        would_promote_count: 6,
        would_promote_rate: 1,
        consecutive_clean_passes: 6,
        confidence: { mean: 0.95, stdev: 0.01, min: 0.94, max: 0.97, stable: true },
        recurring_blockers: {},
        blocker_counts: {},
        thresholds: {},
        regime: 'pre_go_live_calibration',
        is_current_readiness: true,
        ...overrides,
      };
    }

    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('presents a current readiness streak pre-go-live', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ promotion_readiness: makeReadiness() })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByText(/self-approval streak clears thresholds/)).toBeInTheDocument();
      expect(screen.queryByText(/pre-go-live dry-run calibration \(historical\)/)).not.toBeInTheDocument();
    });

    it('relabels as historical calibration once promotion is live (not current readiness)', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            promotion_readiness: makeReadiness({
              ready: false,
              is_current_readiness: false,
              regime: 'live',
              promotion_enabled: true,
              reason: 'promotion is live for this project — this is a pre-go-live dry-run calibration window, not current readiness',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      // Stale dry-run streak is NOT presented as a live "ready" signal.
      expect(screen.queryByText(/self-approval streak clears thresholds/)).not.toBeInTheDocument();
      expect(screen.getByText(/pre-go-live dry-run calibration \(historical\)/)).toBeInTheDocument();
      expect(screen.getByText(/frozen/)).toBeInTheDocument();
    });

    it('shows validated precision badge when ground_truth_validated is true with sufficient sample', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            promotion_readiness: makeReadiness({
              ground_truth_validated: true,
              validated_precision: 0.92,
              resolved_outcome_sample: 12,
              serving_confirmed_count: 11,
              deploy_failed_count: 1,
              caveat: 'validated against post-promotion outcomes',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      // Should show self-approval rate
      expect(screen.getByText(/100% self-approval rate/)).toBeInTheDocument();
      // Should show validated precision badge with sample count
      expect(screen.getByText(/92% validated precision \(12 resolved\)/)).toBeInTheDocument();
      // Caveat should reflect the validated state
      expect(screen.getByText(/validated against post-promotion outcomes/)).toBeInTheDocument();
    });

    it('shows self-approval-only framing when ground_truth_validated is false', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            promotion_readiness: makeReadiness({
              ground_truth_validated: false,
              validated_precision: null,
              resolved_outcome_sample: 0,
              caveat: 'self-approval only — no post-promotion outcome is recorded',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      // Should NOT show validated precision badge
      expect(screen.queryByText(/validated precision/)).not.toBeInTheDocument();
      // Should show self-approval rate
      expect(screen.getByText(/100% self-approval rate/)).toBeInTheDocument();
      // Caveat should reflect self-approval-only
      expect(screen.getByText(/self-approval only/)).toBeInTheDocument();
    });

    it('does not show validated precision badge when sample is zero even if ground_truth_validated is true', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            promotion_readiness: makeReadiness({
              ground_truth_validated: true,
              validated_precision: null,
              resolved_outcome_sample: 0,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      // Should NOT show validated precision badge (no sample yet)
      expect(screen.queryByText(/validated precision/)).not.toBeInTheDocument();
      // Should show self-approval rate
      expect(screen.getByText(/100% self-approval rate/)).toBeInTheDocument();
    });
  });

  describe('post-promotion serving confirmation', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('renders a green serving-confirmed badge with served SHA and time', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted',
                summary: 'Candidate was promoted AND its deploy is CONFIRMED serving.',
                serving_status: 'serving_confirmed',
              },
              serving_status: 'serving_confirmed',
              served_sha: 'abcdef1234567890',
              served_at: '2026-01-02T00:00:00Z',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'serving_confirmed');
      expect(badge).toHaveTextContent(/serving confirmed/);
      expect(badge).toHaveTextContent('@abcdef1');
    });

    it('renders an amber awaiting-confirmation badge with the remaining window while unconfirmed', () => {
      // Deadline 10 minutes in the future — still inside the confirmation window.
      const deadline = new Date(Date.now() + 10 * 60 * 1000).toISOString();
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted_unconfirmed',
                summary: 'Candidate was promoted (merged) but serving is NOT yet confirmed.',
                serving_status: 'unconfirmed',
              },
              serving_status: 'unconfirmed',
              deploy_confirm_deadline: deadline,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'unconfirmed');
      expect(badge).toHaveAttribute('data-deploy-window-overdue', 'false');
      expect(badge).toHaveTextContent(/awaiting deploy confirmation/);
      expect(badge).toHaveTextContent(/left in window/);
      expect(badge.className).toContain('text-amber-400');
    });

    it('renders a red OVERDUE cue when the confirmation window has elapsed while still unconfirmed', () => {
      // Deadline 15 minutes in the past — the resolving sweep is late/stuck.
      const deadline = new Date(Date.now() - 15 * 60 * 1000).toISOString();
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted_unconfirmed',
                summary: 'Candidate was promoted (merged) but serving is NOT yet confirmed.',
                serving_status: 'unconfirmed',
              },
              serving_status: 'unconfirmed',
              deploy_confirm_deadline: deadline,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'unconfirmed');
      expect(badge).toHaveAttribute('data-deploy-window-overdue', 'true');
      expect(badge).toHaveTextContent(/OVERDUE/);
      expect(badge).toHaveTextContent(/window elapsed/);
      expect(badge.className).toContain('text-red-400');
    });

    it('shows the unconfirmed confirmation window in History rows for a recently-promoted version', () => {
      const deadline = new Date(Date.now() + 5 * 60 * 1000).toISOString();
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: null,
            recent_versions: [
              {
                version_number: 5,
                status: 'promoted',
                opened_at: '2026-01-01T00:00:00Z',
                promoted_at: '2026-01-02T00:00:00Z',
                qualification_attempts: 1,
                last_qualification: null,
                serving_status: 'unconfirmed',
                deploy_confirm_deadline: deadline,
              },
            ],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'unconfirmed');
      expect(badge).toHaveAttribute('data-deploy-window-overdue', 'false');
      expect(badge).toHaveTextContent(/left in window/);
    });

    it('renders a loud red failure badge with the reason — not a clean green promoted', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promotion_deploy_failed',
                summary: 'Candidate merged but its deploy NEVER confirmed serving.',
                serving_status: 'deploy_failed',
                deploy_failure_reason: 'boot crash loop',
              },
              serving_status: 'deploy_failed',
              deploy_failure_reason: 'boot crash loop',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'deploy_failed');
      expect(badge).toHaveTextContent(/deploy failed/);
      expect(badge).toHaveTextContent('boot crash loop');
      expect(badge.className).toContain('text-red-400');
      // The deploy-failed state badge must NOT be styled like the green 'promoted'.
      const stateBadge = screen.getByText('promotion deploy failed');
      expect(stateBadge.className).toContain('text-red-400');
      expect(stateBadge.className).not.toContain('emerald');
    });

    it('shows the serving failure in the History rows for a recent version', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: null,
            recent_versions: [
              {
                version_number: 4,
                status: 'promoted',
                opened_at: '2026-01-01T00:00:00Z',
                promoted_at: '2026-01-02T00:00:00Z',
                qualification_attempts: 1,
                last_qualification: null,
                serving_status: 'deploy_failed',
                deploy_failure_reason: 'never stabilized',
              },
            ],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'deploy_failed');
      expect(badge).toHaveTextContent('never stabilized');
    });

    it('reflects post_promotion_outcome in the verdict history', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [
          {
            id: 'v1',
            release_version_id: 'rv1',
            candidate_sha: 'deadbeef1234',
            confidence: 0.95,
            sub_scores: null,
            blocker: null,
            audit_missing_reason: null,
            would_promote: true,
            promotion_enabled: true,
            terminal_status: 'promoted',
            threshold: 0.9,
            started_at: null,
            finalized_at: '2026-01-02T00:00:00Z',
            created_at: '2026-01-02T00:00:00Z',
            post_promotion_outcome: 'deploy_failed',
          },
        ],
        total: 1,
      });
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse()}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const history = await screen.findByTestId('verdict-history');
      expect(within(history).getByText(/deploy failed/)).toBeInTheDocument();
    });
  });

  describe('per-artifact serving evidence', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('drives the headline from the aggregate: backend revision serving but a required artifact pending renders awaiting-confirmation, NOT serving confirmed', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted_unconfirmed',
                summary: 'Promoted but not every required artifact confirms serving yet.',
                serving_status: 'serving_confirmed',
              },
              // Backend-only scalar says serving — the aggregate must override it.
              serving_status: 'serving_confirmed',
              served_sha: 'abcdef1234567890',
              aggregate_serving_status: 'unconfirmed',
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abcdef1234567890', served_at: '2026-01-02T00:00:00Z', reason_code: null },
                { artifact_kind: 'frontend_bundle', required: true, serving_status: 'unconfirmed', served_identifier: null, served_at: null, reason_code: null },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'unconfirmed');
      expect(badge).toHaveTextContent(/awaiting deploy confirmation/);
      expect(badge).not.toHaveTextContent(/serving confirmed/);
    });

    it('aggregate deploy_failed names the failed artifact and reason in the breakdown', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promotion_deploy_failed',
                summary: 'A required artifact failed to confirm serving.',
                serving_status: 'serving_confirmed',
              },
              serving_status: 'serving_confirmed',
              aggregate_serving_status: 'deploy_failed',
              aggregate_deploy_failure_reason: 'frontend bundle 502 on canary',
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abcdef1234567890', served_at: '2026-01-02T00:00:00Z', reason_code: null },
                { artifact_kind: 'frontend_bundle', required: true, serving_status: 'deploy_failed', served_identifier: null, served_at: null, reason_code: 'frontend bundle 502 on canary' },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'deploy_failed');
      expect(badge).toHaveTextContent('frontend bundle 502 on canary');

      const failedRow = screen
        .getAllByTestId('artifact-evidence-row')
        .find((r) => r.getAttribute('data-artifact-kind') === 'frontend_bundle');
      expect(failedRow).toBeDefined();
      expect(failedRow).toHaveAttribute('data-artifact-status', 'deploy_failed');
      expect(failedRow).toHaveTextContent('frontend bundle 502 on canary');
    });

    it('renders one breakdown row per artifact with kind + status', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted_unconfirmed',
                summary: 'Awaiting multi-artifact confirmation.',
                serving_status: 'unconfirmed',
              },
              serving_status: 'unconfirmed',
              aggregate_serving_status: 'unconfirmed',
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abcdef1234567890', served_at: '2026-01-02T00:00:00Z', reason_code: null },
                { artifact_kind: 'frontend_bundle', required: true, serving_status: 'unconfirmed', served_identifier: null, served_at: null, reason_code: null },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const rows = screen.getAllByTestId('artifact-evidence-row');
      expect(rows).toHaveLength(2);
      expect(rows[0]).toHaveAttribute('data-artifact-kind', 'backend_revision');
      expect(rows[0]).toHaveAttribute('data-artifact-status', 'serving_confirmed');
      expect(rows[0]).toHaveTextContent('✓ serving');
      expect(rows[1]).toHaveAttribute('data-artifact-kind', 'frontend_bundle');
      expect(rows[1]).toHaveAttribute('data-artifact-status', 'unconfirmed');
      expect(rows[1]).toHaveTextContent('pending');
    });

    it('recovery: aggregate serving_confirmed with every artifact confirmed renders green', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted',
                summary: 'All required artifacts now confirm serving.',
                serving_status: 'serving_confirmed',
              },
              serving_status: 'serving_confirmed',
              served_sha: 'abcdef1234567890',
              served_at: '2026-01-02T00:00:00Z',
              aggregate_serving_status: 'serving_confirmed',
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abcdef1234567890', served_at: '2026-01-02T00:00:00Z', reason_code: null },
                { artifact_kind: 'frontend_bundle', required: true, serving_status: 'serving_confirmed', served_identifier: 'bundle-42', served_at: '2026-01-02T00:05:00Z', reason_code: null },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'serving_confirmed');
      expect(badge.className).toContain('text-emerald-400');
      const rows = screen.getAllByTestId('artifact-evidence-row');
      expect(rows.every((r) => r.getAttribute('data-artifact-status') === 'serving_confirmed')).toBe(true);
    });

    it('backward compatible: a single backend artifact renders no breakdown list', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted',
                summary: 'Single-artifact promotion confirmed serving.',
                serving_status: 'serving_confirmed',
              },
              serving_status: 'serving_confirmed',
              served_sha: 'abcdef1234567890',
              aggregate_serving_status: 'serving_confirmed',
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abcdef1234567890', served_at: '2026-01-02T00:00:00Z', reason_code: null },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByTestId('serving-status-badge')).toHaveAttribute('data-serving-status', 'serving_confirmed');
      expect(screen.queryByTestId('artifact-evidence-list')).not.toBeInTheDocument();
    });

    it('backward compatible: when the API omits the new fields, the scalar drives the badge unchanged', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted',
                summary: 'Legacy scalar-only response.',
                serving_status: 'serving_confirmed',
              },
              serving_status: 'serving_confirmed',
              served_sha: 'abcdef1234567890',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'serving_confirmed');
      expect(screen.queryByTestId('artifact-evidence-list')).not.toBeInTheDocument();
    });

    it('shows the per-artifact breakdown for a recent (history) version too', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: null,
            recent_versions: [
              {
                version_number: 4,
                status: 'promoted',
                opened_at: '2026-01-01T00:00:00Z',
                promoted_at: '2026-01-02T00:00:00Z',
                qualification_attempts: 1,
                last_qualification: null,
                serving_status: 'serving_confirmed',
                aggregate_serving_status: 'deploy_failed',
                aggregate_deploy_failure_reason: 'bundle never confirmed',
                artifacts: [
                  { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abc1234', served_at: '2026-01-02T00:00:00Z', reason_code: null },
                  { artifact_kind: 'frontend_bundle', required: true, serving_status: 'deploy_failed', served_identifier: null, served_at: null, reason_code: 'bundle never confirmed' },
                ],
              },
            ],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'deploy_failed');
      const rows = screen.getAllByTestId('artifact-evidence-row');
      expect(rows).toHaveLength(2);
      const failed = rows.find((r) => r.getAttribute('data-artifact-kind') === 'frontend_bundle');
      expect(failed).toBeDefined();
      expect(failed).toHaveAttribute('data-artifact-status', 'deploy_failed');
      expect(failed).toHaveTextContent('bundle never confirmed');
    });
  });

  describe('clear deploy-failed lever', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('shows the clear control only for admins when serving_status is deploy_failed', () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({ serving_status: 'deploy_failed' }),
      });
      const { rerender } = render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} userRole="admin" />
      );
      expect(screen.getByTestId('clear-deploy-failed')).toBeInTheDocument();

      // Non-admin: hidden.
      rerender(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} userRole="user" />
      );
      expect(screen.queryByTestId('clear-deploy-failed')).not.toBeInTheDocument();
    });

    it('hides the clear control when no version is deploy_failed', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ serving_status: 'serving_confirmed' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          userRole="admin"
        />
      );
      expect(screen.queryByTestId('clear-deploy-failed')).not.toBeInTheDocument();
    });

    it('hides the clear control for aggregate-only deploy_failed state', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              serving_status: 'serving_confirmed',
              aggregate_serving_status: 'deploy_failed',
              qualification_state: {
                state: 'promotion_deploy_failed',
                summary: 'required artifact failed',
                serving_status: 'deploy_failed',
              },
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          userRole="admin"
        />
      );

      expect(screen.queryByTestId('clear-deploy-failed')).not.toBeInTheDocument();
    });

    it('calls clearDeployFailed on the verify path and reports the outcome', async () => {
      const clearMock = vi.fn().mockResolvedValue({
        version_number: 1,
        serving_status: 'serving_confirmed',
        served_sha: 'live999',
        served_at: '2026-01-01T00:30:00Z',
        deploy_failure_reason: null,
        clear_mode: 'verified',
        operator_attested: false,
        attestation_reason: null,
      });
      vi.mocked(api.clearDeployFailed).mockImplementation(clearMock);

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ serving_status: 'deploy_failed' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          userRole="admin"
        />
      );

      fireEvent.click(screen.getByTestId('clear-deploy-failed'));

      await waitFor(() => {
        expect(clearMock).toHaveBeenCalledWith('proj-1');
      });
      await waitFor(() => {
        expect(screen.getByText(/Deploy-failed cleared \(verified\)/)).toBeInTheDocument();
      });
      expect(mockRefresh).toHaveBeenCalled();
    });

    it('prompts for an attestation reason and force-clears when verify is refused', async () => {
      const clearMock = vi
        .fn()
        .mockRejectedValueOnce(new Error('deploy could not be independently verified live — re-submit with force=true and a reason'))
        .mockResolvedValueOnce({
          version_number: 1,
          serving_status: 'serving_confirmed',
          served_sha: 'promoted123',
          served_at: '2026-01-01T02:00:00Z',
          deploy_failure_reason: null,
          clear_mode: 'operator_attested',
          operator_attested: true,
          attestation_reason: 'verified live by hand',
        });
      vi.mocked(api.clearDeployFailed).mockImplementation(clearMock);
      const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('verified live by hand');

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ serving_status: 'deploy_failed' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          userRole="admin"
        />
      );

      fireEvent.click(screen.getByTestId('clear-deploy-failed'));

      await waitFor(() => {
        expect(promptSpy).toHaveBeenCalled();
      });
      await waitFor(() => {
        expect(clearMock).toHaveBeenLastCalledWith('proj-1', true, 'verified live by hand');
      });
      await waitFor(() => {
        expect(screen.getByText(/operator-attested/)).toBeInTheDocument();
      });
      promptSpy.mockRestore();
    });
  });
});
