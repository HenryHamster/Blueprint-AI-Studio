import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { VersionLifecyclePanel } from './VersionLifecyclePanel';
import * as api from '../api';
import type { VersionLifecycleResponse } from '../api';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn().mockResolvedValue('test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));
vi.mock('../api');

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
    vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
  });

  it('separates lifetime qualifications from the current error retry episode', () => {
    render(
      <VersionLifecyclePanel
        data={makeVersionLifecycleResponse({
          current_version: makeCurrentVersion({
            phase: 'stabilize',
            qualification_attempts: 11,
            qualification_trigger_state: {
              auto_trigger: 'armed',
              last_attempt: null,
              next_expected_trigger: 'sweep_retry',
              blocking: [],
              lifetime_qualification_attempts: 11,
              error_retry_episode: { count: 0, cap: 5 },
              knob: 'on',
              evaluated_at: '2026-01-01T00:00:00Z',
            },
          }),
        })}
        loading={false}
        projectId="proj-1"
        onRefresh={mockRefresh}
      />
    );

    expect(screen.getByText('11 lifetime qualifications')).toBeInTheDocument();
    expect(screen.getByText('lifetime qualifications: 11')).toBeInTheDocument();
    expect(screen.getByText('error retry episode: 0/5')).toBeInTheDocument();
  });

  it('renders the promotion policy source and fallback reason', () => {
    vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

    render(
      <VersionLifecyclePanel
        data={makeVersionLifecycleResponse({
          promotion_policy_source: 'hardcoded_defaults',
          promotion_policy_error: 'ScannerError: promotion_policy.yaml did not parse',
        })}
        loading={false}
        projectId="proj-1"
        onRefresh={mockRefresh}
      />
    );

    expect(screen.getByTestId('promotion-policy-source')).toHaveTextContent('hardcoded_defaults');
    expect(screen.getByTestId('promotion-policy-source-error')).toHaveTextContent('ScannerError: promotion_policy.yaml did not parse');
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

      // ConfirmDialog appears with the fleet-wide warning
      expect(screen.getByText('Enable live promotion for ALL PROJECTS? Passing qualify runs will push to main fleet-wide.')).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

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
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

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

      // ConfirmDialog appears for project-level arming
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

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

      // Should show the inline input and Cut button
      const reasonInput = screen.getByPlaceholderText('Optional reason for cut...');
      expect(reasonInput).toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: /^Cut$/ }));

      // ConfirmDialog appears with the abandon message
      expect(screen.getByText('Abandon v1 and open a fresh version?')).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

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
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

      await waitFor(() => {
        expect(cutVersionMock).toHaveBeenCalledWith('proj-1', 'stalled in build', expect.any(String));
      });
    });

    it('cancels cut version action when cancel is clicked in the dialog', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

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

      // ConfirmDialog appears; click the dialog's Cancel button
      expect(screen.getByTestId('confirm-dialog-confirm')).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));

      expect(vi.mocked(api.cutVersion)).not.toHaveBeenCalled();
    });

    it('shows action message on successful cut version', async () => {
      const cutVersionMock = vi.fn().mockResolvedValue({
        abandoned_version: { version_number: 5, status: 'abandoned' },
        new_version: { version_number: 6, phase: 'build', goal: null, opened_at: '2026-01-02T00:00:00Z' },
      });
      vi.mocked(api.cutVersion).mockImplementation(cutVersionMock);
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

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
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

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
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
      await waitFor(() => expect(cutVersionMock).toHaveBeenCalledTimes(1));

      // Retry after the failure (the cut UI stays open on error, dialog is closed).
      // findByRole waits for the "Cut" button to re-appear after loading clears.
      fireEvent.click(await screen.findByRole('button', { name: /^Cut$/ }));
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
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

  describe("current version what's-new headline", () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    const HIGHLIGHTS: api.VersionSummaryHighlights = {
      headline: 'open in stabilize with 7 merged',
      merged_count: 7,
      proven: 4,
      total_merged_for_acceptance: 7,
      qualification_confidence: 0.84,
      qualification_threshold: 0.9,
    };

    it('renders the headline + at-a-glance metrics for the current version when highlights are present', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ phase: 'stabilize', highlights: HIGHLIGHTS }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const headline = screen.getByTestId('version-headline');
      expect(headline).toHaveTextContent('open in stabilize with 7 merged');
      expect(headline).toHaveTextContent('4/7 proven');
      expect(headline).toHaveTextContent('conf 84% / thr 90%');
      // Status for the current (open) version is always 'open'.
      expect(headline).toHaveTextContent('stabilize · open');
    });

    it('omits the headline when the current version has no highlights (older backend)', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ phase: 'build' }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.queryByTestId('version-headline')).not.toBeInTheDocument();
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

    it('does not assert no tasks in flight when count is unavailable (null)', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ version_number: 2, in_flight_task_count: null }),
            staged_directions: [],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: /Cut\/Abandon/ }));

      const consequences = screen.getByTestId('cut-consequences');
      expect(consequences).toHaveTextContent('In-flight task count unavailable.');
      expect(consequences).not.toHaveTextContent('No tasks are currently in flight.');
    });

    it('folds consequences into the confirm dialog message', async () => {
      vi.mocked(api.cutVersion).mockResolvedValue({
        abandoned_version: { version_number: 6, status: 'abandoned' },
        new_version: { version_number: 7, phase: 'build', goal: null, opened_at: '2026-01-02T00:00:00Z' },
      });

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

      // ConfirmDialog shows the consequences folded into the message
      await waitFor(() => {
        expect(screen.getByTestId('confirm-dialog-confirm')).toBeInTheDocument();
      });
      expect(screen.getByText(/1 staged direction will start now/)).toBeInTheDocument();
      expect(screen.getByText(/2 in-flight tasks will keep running against the new version/)).toBeInTheDocument();
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

      // ConfirmDialog shows the promotion-in-flight context in the message
      await waitFor(() => {
        expect(screen.getByTestId('confirm-dialog-confirm')).toBeInTheDocument();
      });
      expect(screen.getByText(/promotion PR #7357 is open and auto-merging \(in flight ~2h 5m \(auto-timeout cap 4h\)\)/)).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));
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
      expect(within(evidence).getByText(/1 excluded audit finding/)).toBeInTheDocument();
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
      expect(within(evidence).getByText(/1 excluded audit finding/)).toBeInTheDocument();
      expect(within(evidence).getByText(/migration pass/)).toBeInTheDocument();
      expect(within(evidence).getByText(/apply pass/)).toBeInTheDocument();
      expect(within(evidence).getByText(/promotion PR/)).toBeInTheDocument();
      expect(within(evidence).getByTitle('schema migration verified')).toBeInTheDocument();
      expect(within(evidence).getByTitle('apply path verified')).toBeInTheDocument();
    });

    it('renders challenger-refuted false positives with reason in verdict history', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [
          {
            id: 'v2',
            release_version_id: 'rv2',
            candidate_sha: 'aabbcc112233',
            confidence: 0.92,
            sub_scores: null,
            blocker: null,
            audit_missing_reason: null,
            audit_findings: [{ severity: 'high', summary: 'confirmed finding' }],
            excluded_audit_findings: [{ severity: 'low', summary: 'excluded noise' }],
            challenger_refuted_audit_findings: [
              { severity: 'high', summary: 'false alarm finding', challenger_reason: 'already fixed in HEAD' },
              { severity: 'medium', summary: 'another false positive', challenger_reason: 'out of scope' },
            ],
            would_promote: true,
            promotion_enabled: true,
            terminal_status: 'promoted',
            threshold: 0.9,
            started_at: null,
            finalized_at: '2026-01-03T00:00:00Z',
            created_at: '2026-01-03T00:00:00Z',
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
      // Challenger-refuted chip shows count and is distinct from excluded-audit-findings chip
      expect(within(evidence).getByTestId('challenger-refuted-findings')).toBeInTheDocument();
      expect(within(evidence).getByText(/2 challenger false positives/)).toBeInTheDocument();
      // Tooltip includes severity, summary, and reason for each refuted finding
      expect(within(evidence).getByTitle('high: false alarm finding — already fixed in HEAD | medium: another false positive — out of scope')).toBeInTheDocument();
      // The existing excluded-audit-findings chip still renders distinctly
      expect(within(evidence).getByText(/1 excluded audit finding/)).toBeInTheDocument();
      expect(within(evidence).queryByTestId('challenger-refuted-findings')).not.toBe(
        within(evidence).queryByText(/1 excluded audit finding/),
      );
    });

    it('renders challenger-refuted false positives with reason on the open version', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'high', summary: 'confirmed finding' }],
                excluded_audit_findings: [{ severity: 'low', summary: 'excluded noise' }],
                challenger_refuted_audit_findings: [
                  { severity: 'high', summary: 'false alarm', challenger_reason: 'fixed upstream' },
                ],
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      expect(within(evidence).getByTestId('challenger-refuted-findings')).toBeInTheDocument();
      expect(within(evidence).getByText(/1 challenger false positive/)).toBeInTheDocument();
      expect(within(evidence).getByTitle('high: false alarm — fixed upstream')).toBeInTheDocument();

      // Excluded audit finding chip still renders separately
      expect(within(evidence).getByText(/1 excluded audit finding/)).toBeInTheDocument();
    });

    it('does not render challenger chips when challenger_refuted_audit_findings is absent', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'high', summary: 'some finding' }],
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      expect(within(evidence).queryByTestId('challenger-refuted-findings')).not.toBeInTheDocument();
    });

    it('renders goal_alignment aligned chip with grade', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'low', summary: 'minor' }],
                goal_alignment: { state: 'aligned', assessed_grade: 0.9 },
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      const chip = within(evidence).getByTestId('goal-alignment-chip');
      expect(chip).toBeInTheDocument();
      expect(chip).toHaveTextContent('goal aligned 90%');
    });

    it('renders goal_alignment shortfall chip with biggest_gap in title', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'low', summary: 'minor' }],
                goal_alignment: {
                  state: 'shortfall',
                  assessed_grade: 0.3,
                  biggest_gap: 'DB hot-loop over-fetch was not addressed',
                },
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      const chip = within(evidence).getByTestId('goal-alignment-chip');
      expect(chip).toBeInTheDocument();
      expect(chip).toHaveTextContent('goal shortfall 30%');
      expect(chip).toHaveAttribute('title', 'DB hot-loop over-fetch was not addressed');
    });

    it('does not render goal_alignment chip when state is not_applicable', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'low', summary: 'minor' }],
                goal_alignment: { state: 'not_applicable' },
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      expect(within(evidence).queryByTestId('goal-alignment-chip')).not.toBeInTheDocument();
    });

    it('does not render goal_alignment chip when goal_alignment is absent', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'low', summary: 'minor' }],
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      expect(within(evidence).queryByTestId('goal-alignment-chip')).not.toBeInTheDocument();
    });

    it('renders blind_merge_grade covers chip', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'low', summary: 'minor' }],
                blind_merge_grade: { coverage_verdict: 'covers', biggest_gap: 'No significant gap' },
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      const chip = within(evidence).getByTestId('blind-merge-grade-chip');
      expect(chip).toBeInTheDocument();
      expect(chip).toHaveTextContent('merge covers');
      expect(chip).toHaveAttribute('title', 'No significant gap');
    });

    it('renders blind_merge_grade misses chip with red styling', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'low', summary: 'minor' }],
                blind_merge_grade: { coverage_verdict: 'misses', biggest_gap: 'Core feature not implemented' },
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      const chip = within(evidence).getByTestId('blind-merge-grade-chip');
      expect(chip).toBeInTheDocument();
      expect(chip).toHaveTextContent('merge misses');
      expect(chip).toHaveAttribute('title', 'Core feature not implemented');
    });

    it('renders blind_merge_grade partial chip with biggest_gap in title', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'low', summary: 'minor' }],
                blind_merge_grade: { coverage_verdict: 'partial', biggest_gap: 'DB migration not addressed' },
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      const chip = within(evidence).getByTestId('blind-merge-grade-chip');
      expect(chip).toBeInTheDocument();
      expect(chip).toHaveTextContent('merge partial');
      expect(chip).toHaveAttribute('title', 'DB migration not addressed');
    });

    it('does not render blind_merge_grade chip when blind_merge_grade is absent', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                audit_findings: [{ severity: 'low', summary: 'minor' }],
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const evidence = screen.getByTestId('durable-qualification-evidence');
      expect(within(evidence).queryByTestId('blind-merge-grade-chip')).not.toBeInTheDocument();
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

    it('suppresses the merged_since_verdict badge when qualification is already running', () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'abcdef1234567890' }),
              merged_since_verdict: 3,
              qualification_state: { state: 'qualifying', summary: 'Audit in flight.' },
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByText(/merged since this verdict/)).not.toBeInTheDocument();
    });

    it('shows the merged_since_verdict badge when qualification is not running', () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: 'abcdef1234567890' }),
              merged_since_verdict: 3,
              qualification_state: { state: 'not_promoted', summary: 'Did not meet promotion criteria.' },
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

  describe('managed promotion check-wait locks qualify + phase controls', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    function makeCheckWaitState(
      overrides: Partial<api.QualificationState> = {},
    ): api.QualificationState {
      return {
        state: 'promotion_check_wait',
        summary:
          'Audit PASSED; a migration-carrying promotion PR is open. Blueprint is WAITING for required status checks and will retry the merge AUTOMATICALLY.',
        promotion_pr_number: 9191,
        promotion_pr_url: 'https://example/pr/9191',
        migration_carrying: true,
        ci_state: 'pending',
        ...overrides,
      };
    }

    // Plan-time acceptance target: the managed check-wait state must lock the
    // re-qualify affordance (no fresh qualification needed/possible) and present
    // copy distinct from native auto-merge.
    it('renders_managed_promotion_check_wait_without_requalify_affordance', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: makeCheckWaitState(),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      // The Run Qualify control is present but LOCKED — there is no actionable
      // re-qualify affordance while Blueprint owns the retry.
      const qualifyBtn = screen.getByRole('button', { name: /Run Qualify/ });
      expect(qualifyBtn).toBeDisabled();
      expect(qualifyBtn).toHaveAttribute(
        'title',
        'A migration-carrying promotion PR is open and waiting for required checks — Blueprint retries the merge automatically; wait for it to land or cancel it before re-qualifying'
      );
      // The phase flip is locked too.
      expect(screen.getByRole('button', { name: /build/ })).toBeDisabled();

      // Distinct check-wait note (NOT the native auto-merge note) carries the
      // truth: waiting for required checks, auto-retry, no fresh qualify needed.
      const note = screen.getByTestId('promotion-check-wait-note');
      expect(note).toHaveTextContent('#9191');
      expect(note).toHaveTextContent('waiting for required checks');
      expect(note).toHaveTextContent('no fresh qualification needed');
      // The native auto-merge note must NOT render for this state.
      expect(screen.queryByTestId('promotion-in-flight-note')).not.toBeInTheDocument();
      // The badge renders with the distinct check-wait label.
      expect(screen.getByText('promotion check wait')).toBeInTheDocument();
    });

    it('surfaces the check-wait freshness with auto-retry copy', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: makeCheckWaitState({
                promotion_age_seconds: 1800,
                promotion_timeout_seconds: 14400,
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByTestId('promotion-check-wait-freshness')).toHaveTextContent(
        'Migration-carrying promotion PR #9191 waiting for required checks (in flight ~30m (auto-timeout cap 4h)) — Blueprint retries the merge automatically'
      );
      // The native in-flight freshness must NOT render.
      expect(screen.queryByTestId('promotion-in-flight-freshness')).not.toBeInTheDocument();
    });

    it('renders managed check-wait snapshot details', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: makeCheckWaitState({
                check_snapshot: {
                  state: 'pending',
                  classified_at: '2026-01-01T00:20:00Z',
                  head_sha: 'cafebabecafebabecafebabecafebabecafebabe',
                  total_checks: 12,
                  checks: [
                    { name: 'CI / Backend', status: 'in_progress' },
                    { name: 'CI / Agent', status: 'queued' },
                  ],
                  truncated: true,
                },
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const snapshot = screen.getByTestId('promotion-check-wait-snapshot');
      expect(snapshot).toHaveTextContent('ci:pending');
      expect(snapshot).toHaveTextContent('head cafebab');
      expect(snapshot).toHaveTextContent('12 checks');
      expect(snapshot).toHaveTextContent('CI / Backend: in_progress');
      expect(snapshot).toHaveTextContent('CI / Agent: queued');
      expect(snapshot).toHaveTextContent('+10 more');
    });

    it('renders managed check-wait no-checks snapshot as absent CI', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: makeCheckWaitState({
                ci_state: 'no_checks',
                check_snapshot: {
                  state: 'no_checks',
                  classified_at: '2026-01-01T00:20:00Z',
                  head_sha: '1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa',
                  total_checks: 0,
                  checks: [],
                  truncated: false,
                },
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const snapshot = screen.getByTestId('promotion-check-wait-snapshot');
      expect(snapshot).toHaveTextContent('ci:no_checks');
      expect(snapshot).toHaveTextContent('0 checks');
      expect(snapshot).toHaveTextContent('No check runs reported yet.');
    });
  });

  describe('promotion blocker classification copy', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('renders required_check_failed detail in red', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: {
                state: 'required_check_failed',
                summary: 'The promotion PR required checks FAILED.',
                error: 'required_check_failed',
                detail: 'PR #9191 required checks failed: CI / Backend',
              },
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByTestId('promotion-blocker-detail')).toHaveTextContent('CI / Backend');
      // A failed blocker is NOT a lock — qualify stays actionable to retry.
      expect(screen.getByRole('button', { name: /Run Qualify/ })).toBeEnabled();
    });

    it('renders protection_blocked_review detail in orange and keeps qualify enabled', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              phase: 'stabilize',
              qualification_state: {
                state: 'protection_blocked_review',
                summary: 'Branch protection refused the merge; a human action is required.',
                error: 'protection_blocked_review',
                detail: 'At least 1 approving review is required',
              },
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const detail = screen.getByTestId('promotion-blocker-detail');
      expect(detail).toHaveTextContent('approving review');
      expect(detail.className).toContain('text-orange-400');
      expect(screen.getByRole('button', { name: /Run Qualify/ })).toBeEnabled();
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

    it('renders acceptance caveat on ready badge when present', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            promotion_readiness: makeReadiness({
              ready: true,
              ground_truth_validated: true,
              validated_precision: 0.9,
              resolved_outcome_sample: 10,
              serving_confirmed_count: 9,
              deploy_failed_count: 1,
              acceptance_caveat: 'acceptance not fully proven — this version contains 2 satisfied-but-not-machine-proven task(s)',
              caveat: 'validated against post-promotion outcomes',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      // Badge should be green (ready=true)
      expect(screen.getByTestId('ready-badge')).toHaveClass('bg-emerald-500/10');
      // Acceptance caveat should be displayed despite ready=true
      expect(screen.getByTestId('acceptance-caveat-on-badge')).toBeInTheDocument();
      expect(screen.getByTestId('acceptance-caveat-on-badge')).toHaveTextContent(/acceptance not fully proven/);
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

    it('renders an amber timed-out badge (NOT a red failed badge) when confirmation timed out', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promotion_confirmation_timed_out',
                summary: 'Serving confirmation TIMED OUT — not a failed deploy.',
                serving_status: 'confirmation_timed_out',
                deploy_failure_reason: 'serving_sha_unobservable',
              },
              serving_status: 'confirmation_timed_out',
              deploy_failure_reason: 'serving_sha_unobservable',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('serving-status-badge');
      expect(badge).toHaveAttribute('data-serving-status', 'confirmation_timed_out');
      expect(badge).toHaveTextContent(/confirmation timed out/);
      expect(badge).toHaveTextContent('serving_sha_unobservable');
      // A timeout is unverified (amber), NOT a failure (red).
      expect(badge.className).toContain('text-amber-400');
      expect(badge.className).not.toContain('text-red-400');
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
                serving: { status: 'unconfirmed' },
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
                serving: { status: 'deploy_failed', failure_reason: 'never stabilized' },
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

    it('renders serving_confirmed outcome badge in verdict history', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [
          {
            id: 'v2',
            release_version_id: 'rv1',
            candidate_sha: 'aabbccdd1234',
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
            post_promotion_outcome: 'serving_confirmed',
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
      const badge = within(history).getByTestId('post-promotion-outcome-badge');
      expect(badge).toHaveAttribute('data-outcome', 'serving_confirmed');
      expect(badge).toHaveTextContent('✓ serving');
      expect(badge.className).toContain('text-emerald-400');
    });

    it('renders awaiting-outcome badge for promoted verdict with null outcome in verdict history', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [
          {
            id: 'v3',
            release_version_id: 'rv1',
            candidate_sha: 'eeff11221234',
            confidence: 0.92,
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
            post_promotion_outcome: null,
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
      const badge = within(history).getByTestId('post-promotion-outcome-badge');
      expect(badge).toHaveAttribute('data-outcome', 'awaiting');
      expect(badge).toHaveTextContent('awaiting outcome');
      expect(badge.className).toContain('text-amber-400');
    });

    it('shows no outcome badge for non-promoted verdicts', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [
          {
            id: 'v4',
            release_version_id: 'rv1',
            candidate_sha: 'ff001122abcd',
            confidence: 0.75,
            sub_scores: null,
            blocker: 'confidence below threshold',
            audit_missing_reason: null,
            would_promote: false,
            promotion_enabled: true,
            terminal_status: 'not_promoted',
            threshold: 0.9,
            started_at: null,
            finalized_at: '2026-01-02T00:00:00Z',
            created_at: '2026-01-02T00:00:00Z',
            post_promotion_outcome: null,
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
      expect(within(history).queryByTestId('post-promotion-outcome-badge')).not.toBeInTheDocument();
    });

    it('renders post-promotion outcome for current candidate matched by candidate_sha', async () => {
      const candidateSha = 'abcdef1234567890';
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [
          {
            id: 'v5',
            release_version_id: 'rv1',
            candidate_sha: candidateSha,
            confidence: 0.93,
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
            post_promotion_outcome: 'serving_confirmed',
          },
        ],
        total: 1,
      });
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({ candidate_sha: candidateSha }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      // Wait for verdicts to load, then verify the badge appears in the last_qualification block
      await waitFor(() => {
        const badges = screen.getAllByTestId('post-promotion-outcome-badge');
        // Should have at least one badge (the one from current candidate cross-reference)
        expect(badges.length).toBeGreaterThanOrEqual(1);
        const servingBadge = badges.find(b => b.getAttribute('data-outcome') === 'serving_confirmed');
        expect(servingBadge).toBeDefined();
      });
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
              // Backend resolves the aggregate into `serving.status` — overrides the scalar.
              serving: { status: 'unconfirmed' },
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
              serving: { status: 'deploy_failed', failure_reason: 'frontend bundle 502 on canary' },
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
                serving: { status: 'deploy_failed', failure_reason: 'bundle never confirmed' },
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

      // ConfirmDialog appears — click confirm to proceed with the verify path
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

      await waitFor(() => {
        expect(clearMock).toHaveBeenCalledWith('proj-1');
      });
      await waitFor(() => {
        expect(screen.getByText(/Deploy-failed cleared \(verified\)/)).toBeInTheDocument();
      });
      expect(mockRefresh).toHaveBeenCalled();
    });

    it('prompts for an attestation reason via dialog and force-clears when verify is refused', async () => {
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

      // First dialog: plain confirm to attempt verify path
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

      // First API call fails with force=true required → attestation dialog appears
      await waitFor(() => {
        expect(screen.getByTestId('confirm-dialog-attestation-input')).toBeInTheDocument();
      });
      fireEvent.change(screen.getByTestId('confirm-dialog-attestation-input'), {
        target: { value: 'verified live by hand' },
      });
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

      await waitFor(() => {
        expect(clearMock).toHaveBeenLastCalledWith('proj-1', true, 'verified live by hand');
      });
      await waitFor(() => {
        expect(screen.getByText(/operator-attested/)).toBeInTheDocument();
      });
    });
  });

  describe('ChallengedVsUnchallengedSummary', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('renders nothing when challenged_vs_unchallenged is absent', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse()}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByTestId('challenged-vs-unchallenged')).not.toBeInTheDocument();
    });

    it('renders insufficient-sample state when both cohorts lack data', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            challenged_vs_unchallenged: {
              challenged: { sample_size: 1, insufficient_sample: true },
              unchallenged: { sample_size: 2, insufficient_sample: true },
              total_verdicts: 3,
            },
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.getByTestId('challenged-vs-unchallenged')).toBeInTheDocument();
      expect(screen.getByText(/not enough data yet/)).toBeInTheDocument();
    });

    it('renders cohort stats with exclusion and disagreement rates', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            challenged_vs_unchallenged: {
              challenged: {
                sample_size: 10,
                insufficient_sample: false,
                exclusion_rate: 0.3,
                exclusion_count: 3,
                disagreement_rate: 0.25,
                disagreement_count: 2,
                wrong_decision_rate: 0.1,
                wrong_decision_resolved_sample: 10,
              },
              unchallenged: {
                sample_size: 15,
                insufficient_sample: false,
                exclusion_rate: 0.2,
                exclusion_count: 3,
                wrong_decision_rate: 0.4,
                wrong_decision_resolved_sample: 5,
              },
              total_verdicts: 25,
            },
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const panel = screen.getByTestId('challenged-vs-unchallenged');
      expect(panel).toBeInTheDocument();
      expect(panel).toHaveTextContent('25 verdicts');
      expect(panel).toHaveTextContent('challenged (10)');
      expect(panel).toHaveTextContent('excl 30%');
      expect(panel).toHaveTextContent('disagree 25%');
      expect(panel).toHaveTextContent('unchallenged (15)');
      expect(panel).toHaveTextContent('excl 20%');
    });

    it('renders wrong_decision_rate as zero-rate emerald when no wrong decisions', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            challenged_vs_unchallenged: {
              challenged: {
                sample_size: 5,
                insufficient_sample: false,
                exclusion_rate: 0.0,
                disagreement_rate: 0.0,
                wrong_decision_rate: 0.0,
                wrong_decision_resolved_sample: 5,
              },
              unchallenged: { sample_size: 1, insufficient_sample: true },
              total_verdicts: 6,
            },
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const panel = screen.getByTestId('challenged-vs-unchallenged');
      expect(panel).toHaveTextContent('wrong 0%');
    });

    it('renders challenge_refuted_rate chip on challenged cohort when present', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            challenged_vs_unchallenged: {
              challenged: {
                sample_size: 4,
                insufficient_sample: false,
                exclusion_rate: 0.25,
                exclusion_count: 1,
                disagreement_rate: 0.5,
                disagreement_count: 2,
                challenge_refuted_count: 6,
                challenge_refuted_rate: 0.5,
              },
              unchallenged: {
                sample_size: 4,
                insufficient_sample: false,
                exclusion_rate: 0.0,
                exclusion_count: 0,
              },
              total_verdicts: 8,
            },
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const panel = screen.getByTestId('challenged-vs-unchallenged');
      // challenged cohort shows refuted chip
      expect(panel).toHaveTextContent('refuted 50%');
      // unchallenged cohort has no challenge_refuted fields — chip absent
      const chips = panel.querySelectorAll('span[title*="challenger-refuted"]');
      expect(chips.length).toBe(1);
    });

    it('does not render challenge_refuted_rate chip when field is absent', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            challenged_vs_unchallenged: {
              challenged: {
                sample_size: 3,
                insufficient_sample: false,
                exclusion_rate: 0.0,
                disagreement_rate: 0.0,
              },
              unchallenged: { sample_size: 1, insufficient_sample: true },
              total_verdicts: 4,
            },
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const panel = screen.getByTestId('challenged-vs-unchallenged');
      expect(panel).not.toHaveTextContent('refuted');
    });
  });

  describe('out-of-band promotion badge', () => {
    const oobPromotion: api.OutOfBandPromotion = {
      source: 'operator_override',
      recorded_at: '2026-06-01T12:00:00Z',
      candidate_sha: 'abc1234567890def',
      promoted_sha: 'def7890abc123456',
    };

    it('renders badge on current_version when out_of_band_promotion is set', () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ out_of_band_promotion: oobPromotion }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('out-of-band-promotion-badge');
      expect(badge).toBeInTheDocument();
      expect(badge).toHaveTextContent('promoted out-of-band (operator override)');
      expect(badge).toHaveTextContent('abc1234');
      expect(badge).toHaveTextContent('def7890');
    });

    it('does not render badge on current_version when out_of_band_promotion is null', () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ out_of_band_promotion: null }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByTestId('out-of-band-promotion-badge')).not.toBeInTheDocument();
    });

    it('renders badge on a recent_versions history row when out_of_band_promotion is set', () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      const recentVersion: api.RecentVersion = {
        version_number: 1,
        status: 'promoted',
        opened_at: '2026-01-01T00:00:00Z',
        promoted_at: '2026-06-01T12:00:00Z',
        qualification_attempts: 1,
        last_qualification: null,
        out_of_band_promotion: oobPromotion,
      };
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ version_number: 2 }),
            recent_versions: [recentVersion],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const badge = screen.getByTestId('out-of-band-promotion-badge');
      expect(badge).toBeInTheDocument();
      expect(badge).toHaveTextContent('promoted out-of-band (operator override)');
      expect(badge).toHaveTextContent('abc1234');
      expect(badge).toHaveTextContent('def7890');
    });

    it('does not render badge on history row when out_of_band_promotion is absent', () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
      const recentVersion: api.RecentVersion = {
        version_number: 1,
        status: 'promoted',
        opened_at: '2026-01-01T00:00:00Z',
        promoted_at: '2026-06-01T12:00:00Z',
        qualification_attempts: 1,
        last_qualification: null,
      };
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ version_number: 2 }),
            recent_versions: [recentVersion],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByTestId('out-of-band-promotion-badge')).not.toBeInTheDocument();
    });
  });

  describe('pinned serving version callout', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    it('renders pinned callout for the serving_confirmed version in recent_versions', () => {
      const servingVersion: api.RecentVersion = {
        version_number: 3,
        status: 'promoted',
        opened_at: '2026-01-01T00:00:00Z',
        promoted_at: '2026-06-01T12:00:00Z',
        qualification_attempts: 1,
        last_qualification: null,
        serving: { status: 'serving_confirmed', sha: 'abc1234', confirmed_at: '2026-06-01T13:00:00Z', failure_reason: null, artifacts: null },
        merged_count: 5,
        highlights: { headline: 'shipped 5 improvements', merged_count: 5, proven: 4, total_merged_for_acceptance: 5, qualification_confidence: 0.92, qualification_threshold: 0.9 },
      };
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ version_number: 4 }),
            recent_versions: [servingVersion],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      const callout = screen.getByTestId('pinned-serving-version');
      expect(callout).toBeInTheDocument();
      expect(callout).toHaveTextContent('currently serving');
      expect(callout).toHaveTextContent('v3');
      expect(callout).toHaveTextContent('5 merged');
      expect(callout).toHaveTextContent('shipped 5 improvements');
    });

    it('does not render pinned callout when no version is serving_confirmed', () => {
      const nonServingVersion: api.RecentVersion = {
        version_number: 2,
        status: 'promoted',
        opened_at: '2026-01-01T00:00:00Z',
        promoted_at: '2026-06-01T12:00:00Z',
        qualification_attempts: 1,
        last_qualification: null,
        serving_status: 'unconfirmed',
      };
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({ version_number: 3 }),
            recent_versions: [nonServingVersion],
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByTestId('pinned-serving-version')).not.toBeInTheDocument();
    });
  });

  describe('historical audit context (stale audit)', () => {
    it('renders historical-audit-context with active vs audited SHA when audit_evidence_state is stale', () => {
      const staleAudit: api.LatestAudit = {
        audit_task_id: 'task-old',
        task_status: 'COMPLETED',
        candidate_sha: 'aabbcc1234567890',
        created_at: '2026-01-01T00:00:00Z',
        report: { findings: [] },
      };
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              audit_evidence_state: 'stale',
              stale_audit: staleAudit,
              active_candidate_sha: 'deadbeef12345678',
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const ctx = screen.getByTestId('historical-audit-context');
      expect(ctx).toBeInTheDocument();
      expect(ctx).toHaveTextContent('no current audit for this candidate');
      expect(ctx).toHaveTextContent('@deadbee');
      expect(ctx).toHaveTextContent('@aabbcc1');
    });

    it('does not render historical-audit-context when audit_evidence_state is current', () => {
      const currentAudit: api.LatestAudit = {
        audit_task_id: 'task-current',
        task_status: 'COMPLETED',
        candidate_sha: 'deadbeef12345678',
        created_at: '2026-01-01T00:00:00Z',
        report: { findings: [] },
      };
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              audit_evidence_state: 'current',
              latest_audit: currentAudit,
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.queryByTestId('historical-audit-context')).not.toBeInTheDocument();
    });
  });

  describe('agent_image fleet match annotation', () => {
    beforeEach(() => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({ verdicts: [], total: 0 });
    });

    function makeAgentServingShas(buckets: Partial<api.AgentServingShaBucket>[]): api.AgentServingShasResponse {
      return {
        fleet: {
          total_observed: buckets.reduce((s, b) => s + (b.count ?? 0), 0),
          unknown_count: 0,
          mixed: false,
          buckets: buckets.map((b) => ({
            source_sha: b.source_sha ?? 'sha-a',
            count: b.count ?? 1,
            warm_pool_count: 0,
            task_vm_count: 0,
            image_names: [],
            latest_build_ts: null,
            last_heartbeat_at: null,
            matches_active_candidate: b.matches_active_candidate,
          })),
        },
        current_image: null,
        backend_served_sha: null,
        active_candidate_sha: 'sha-a',
        integration_branch: null,
        integration_head_sha: null,
        promotion_target: null,
        promotion_head_sha: null,
      };
    }

    it('shows fleet VM count for agent_image artifact when serving is pending', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted_unconfirmed',
                summary: 'Awaiting confirmation.',
                serving_status: 'unconfirmed',
              },
              serving: { status: 'unconfirmed' },
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abc', served_at: '2026-01-01T00:00:00Z', reason_code: null },
                { artifact_kind: 'agent_image', required: true, serving_status: 'unconfirmed', served_identifier: null, served_at: null, reason_code: null },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          agentServingShas={makeAgentServingShas([
            { source_sha: 'sha-a', count: 2, matches_active_candidate: true },
            { source_sha: 'sha-b', count: 3, matches_active_candidate: false },
          ])}
        />
      );
      const rows = screen.getAllByTestId('artifact-evidence-row');
      const agentRow = rows.find((r) => r.getAttribute('data-artifact-kind') === 'agent_image');
      expect(agentRow).toBeDefined();
      expect(agentRow?.querySelector('[data-testid="fleet-match-count"]')).toHaveTextContent('2 VMs on candidate');
    });

    it('shows 0 VMs on candidate when no fleet VMs match', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted_unconfirmed',
                summary: 'Awaiting confirmation.',
                serving_status: 'unconfirmed',
              },
              serving: { status: 'unconfirmed' },
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abc', served_at: '2026-01-01T00:00:00Z', reason_code: null },
                { artifact_kind: 'agent_image', required: true, serving_status: 'unconfirmed', served_identifier: null, served_at: null, reason_code: null },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          agentServingShas={makeAgentServingShas([
            { source_sha: 'sha-b', count: 5, matches_active_candidate: false },
          ])}
        />
      );
      const rows = screen.getAllByTestId('artifact-evidence-row');
      const agentRow = rows.find((r) => r.getAttribute('data-artifact-kind') === 'agent_image');
      expect(agentRow).toBeDefined();
      expect(agentRow?.querySelector('[data-testid="fleet-match-count"]')).toHaveTextContent('0 VMs on candidate');
    });

    it('omits fleet match annotation when agentServingShas is not provided', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                state: 'promoted_unconfirmed',
                summary: 'Awaiting confirmation.',
                serving_status: 'unconfirmed',
              },
              serving: { status: 'unconfirmed' },
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abc', served_at: '2026-01-01T00:00:00Z', reason_code: null },
                { artifact_kind: 'agent_image', required: true, serving_status: 'unconfirmed', served_identifier: null, served_at: null, reason_code: null },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );
      expect(screen.queryByTestId('fleet-match-count')).not.toBeInTheDocument();
    });

    it('omits fleet match annotation for agent_image when already serving_confirmed', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              qualification_state: {
                // 'promoted' is the real backend state once a candidate merges;
                // serving confirmation is carried by serving_status, not the
                // qualification state string. ('promoted_serving' was never a
                // value summarize_qualification_state emits.)
                state: 'promoted',
                summary: 'All artifacts confirmed.',
                serving_status: 'serving_confirmed',
              },
              serving: { status: 'serving_confirmed' },
              artifacts: [
                { artifact_kind: 'backend_revision', required: true, serving_status: 'serving_confirmed', served_identifier: 'abc', served_at: '2026-01-01T00:00:00Z', reason_code: null },
                { artifact_kind: 'agent_image', required: true, serving_status: 'serving_confirmed', served_identifier: 'img-1', served_at: '2026-01-01T00:01:00Z', reason_code: null },
              ],
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
          agentServingShas={makeAgentServingShas([
            { source_sha: 'sha-a', count: 5, matches_active_candidate: true },
          ])}
        />
      );
      expect(screen.queryByTestId('fleet-match-count')).not.toBeInTheDocument();
    });
  });

  describe('confidence sparkline', () => {
    function makeVerdict(confidence: number | null, wouldPromote: boolean = true): api.QualificationVerdictRecord {
      return {
        id: `v-${Math.random()}`,
        release_version_id: 'rv1',
        candidate_sha: 'abcdef123456',
        confidence,
        sub_scores: null,
        blocker: null,
        audit_missing_reason: null,
        would_promote: wouldPromote,
        promotion_enabled: false,
        terminal_status: wouldPromote ? 'promoted' : 'not_promoted',
        threshold: 0.9,
        started_at: null,
        finalized_at: '2026-01-01T00:00:00Z',
        created_at: '2026-01-01T00:00:00Z',
        debt_enforce_confidence_preview: 0.81,
        debt_enforce_weight_preview: 0.15,
      };
    }

    it('renders confidence sparkline when verdicts with non-null confidence exist', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [makeVerdict(0.95), makeVerdict(0.88), makeVerdict(0.91)],
        total: 3,
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
      expect(history).toBeInTheDocument();
      expect(screen.getByTestId('confidence-sparkline')).toBeInTheDocument();
    });

    it('omits sparkline when all confidences are null', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [makeVerdict(null), makeVerdict(null)],
        total: 2,
      });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse()}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      await screen.findByTestId('verdict-history');
      expect(screen.queryByTestId('confidence-sparkline')).not.toBeInTheDocument();
    });

    it('omits sparkline when fewer than 2 verdicts have non-null confidence', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [makeVerdict(0.9), makeVerdict(null)],
        total: 2,
      });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse()}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      await screen.findByTestId('verdict-history');
      expect(screen.queryByTestId('confidence-sparkline')).not.toBeInTheDocument();
    });

    it('renders debt-enforce previews in the current summary and verdict history rows', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [makeVerdict(0.91)],
        total: 1,
      });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({
            current_version: makeCurrentVersion({
              last_qualification: makeQualification({
                debt_enforce_confidence_preview: 0.73,
                debt_enforce_weight_preview: 0.15,
              }),
            }),
          })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      await screen.findByTestId('verdict-history');
      const previews = screen.getAllByTestId('debt-enforce-preview');
      expect(previews).toHaveLength(2);
      expect(previews[0]).toHaveTextContent('enforce 73% @w0.15');
      expect(previews[1]).toHaveTextContent('enforce 81% @w0.15');
    });
  });

  describe('changelog toggle', () => {
    const TASKS: api.ChangelogEntry[] = [
      { task_type: 'create', title: 'Add feature A', pr_number: 1, pr_url: null },
      { task_type: 'fix', title: 'Fix bug B', pr_number: 2, pr_url: null },
    ];

    it('hides VersionSummaryPanel by default when tasks_merged is present', () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({ tasks_merged: TASKS, merged_task_count: 2 }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      expect(screen.getByTestId('changelog-toggle')).toBeInTheDocument();
      expect(screen.queryByTestId('version-summary-panel')).not.toBeInTheDocument();
    });

    it('expands VersionSummaryPanel when toggle is clicked', async () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({ tasks_merged: TASKS, merged_task_count: 2 }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      fireEvent.click(screen.getByTestId('changelog-toggle'));
      expect(await screen.findByTestId('version-summary-panel')).toBeInTheDocument();
    });

    it('shows has_more indicator when tasks_merged_has_more is true', () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 75,
          tasks_merged_has_more: true,
        }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      expect(screen.getByTestId('changelog-toggle').textContent).toContain('showing 50 newest');
    });

    it('omits changelog toggle when tasks_merged is undefined', () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({ tasks_merged: undefined }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      expect(screen.queryByTestId('changelog-toggle')).not.toBeInTheDocument();
    });

    it('renders criterion coverage panel with each criterion when criterion_coverage has criteria', async () => {
      const coverage: api.VersionCriterionCoverage = {
        total: 3,
        covered: 1,
        partial: 1,
        uncovered: 1,
        criteria: [
          { criterion_key: 'k1', text: 'Must pass all tests', coverage: 'covered', evidence_count: 2 },
          { criterion_key: 'k2', text: 'Performance within budget', coverage: 'partial', evidence_count: 1 },
          { criterion_key: 'k3', text: 'Security audit complete', coverage: 'uncovered', evidence_count: 0 },
        ],
      };
      const TASKS: api.ChangelogEntry[] = [
        { task_type: 'create', title: 'Add feature', pr_number: 1, pr_url: null },
      ];
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
          criterion_coverage: coverage,
        }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      fireEvent.click(screen.getByTestId('changelog-toggle'));
      expect(await screen.findByTestId('criterion-coverage-panel')).toBeInTheDocument();
      expect(screen.getByText('Must pass all tests')).toBeInTheDocument();
      expect(screen.getByText('Performance within budget')).toBeInTheDocument();
      expect(screen.getByText('Security audit complete')).toBeInTheDocument();
      expect(screen.getByTestId('criterion-row-k1')).toBeInTheDocument();
      expect(screen.getByTestId('criterion-row-k2')).toBeInTheDocument();
      expect(screen.getByTestId('criterion-row-k3')).toBeInTheDocument();
    });

    it('renders inactive criterion audit rows from current_version criterion_coverage when active total is zero', async () => {
      const coverage: api.VersionCriterionCoverage = {
        total: 0,
        covered: 0,
        partial: 0,
        uncovered: 0,
        retracted: 1,
        superseded: 0,
        inactive: 1,
        criteria: [
          {
            criterion_key: 'inactive-k1',
            text: 'Retracted criterion remains visible',
            coverage: 'uncovered',
            status: 'retracted',
            evidence_count: 0,
          },
        ],
      };
      const TASKS: api.ChangelogEntry[] = [
        { task_type: 'create', title: 'Add feature', pr_number: 1, pr_url: null },
      ];
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
          criterion_coverage: coverage,
        }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      fireEvent.click(screen.getByTestId('changelog-toggle'));
      expect(await screen.findByTestId('criterion-row-inactive-k1')).toHaveTextContent('Retracted criterion remains visible');
      expect(screen.getByTestId('criterion-status-inactive-k1')).toHaveTextContent('retracted');
      expect(screen.queryByText('No acceptance criteria defined for this version.')).not.toBeInTheDocument();
    });

    it('does not render criterion coverage panel when criterion_coverage is null', async () => {
      const TASKS: api.ChangelogEntry[] = [
        { task_type: 'create', title: 'Add feature', pr_number: 1, pr_url: null },
      ];
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
          criterion_coverage: null,
        }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      fireEvent.click(screen.getByTestId('changelog-toggle'));
      await screen.findByTestId('version-summary-panel');
      expect(screen.queryByTestId('criterion-coverage-panel')).not.toBeInTheDocument();
    });

    it('does not render criterion coverage panel when criterion_coverage is absent', async () => {
      const TASKS: api.ChangelogEntry[] = [
        { task_type: 'create', title: 'Add feature', pr_number: 1, pr_url: null },
      ];
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
        }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      fireEvent.click(screen.getByTestId('changelog-toggle'));
      await screen.findByTestId('version-summary-panel');
      expect(screen.queryByTestId('criterion-coverage-panel')).not.toBeInTheDocument();
    });
  });

  describe('smoketest disabled indicator', () => {
    it('renders "smoke disabled" instead of "smoke 0%" when smoketest_enabled is false on last_qualification', () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          last_qualification: makeQualification({
            sub_scores: { audit: 0.90, test_erosion: 0.85, smoketest: 0.0 },
            smoketest_enabled: false,
          }),
        }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      expect(screen.getByText('smoke disabled')).toBeInTheDocument();
      expect(screen.queryByText(/smoke \d+%/)).not.toBeInTheDocument();
    });

    it('renders "smoke XX%" normally when smoketest_enabled is true', () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          last_qualification: makeQualification({
            sub_scores: { audit: 0.90, test_erosion: 0.85, smoketest: 0.80 },
            smoketest_enabled: true,
          }),
        }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      expect(screen.getByText('smoke 80%')).toBeInTheDocument();
      expect(screen.queryByText('smoke disabled')).not.toBeInTheDocument();
    });

    it('renders "smoke XX%" normally when smoketest_enabled is absent (legacy record)', () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          last_qualification: makeQualification({
            sub_scores: { audit: 0.90, test_erosion: 0.85, smoketest: 0.75 },
          }),
        }),
      });

      render(
        <VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />
      );

      expect(screen.getByText('smoke 75%')).toBeInTheDocument();
      expect(screen.queryByText('smoke disabled')).not.toBeInTheDocument();
    });

    it('renders "s:—" in verdict history sub-scores when smoketest_enabled is false', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [
          {
            id: 'v-1',
            release_version_id: 'rv1',
            candidate_sha: 'abcdef123456',
            confidence: 0.88,
            sub_scores: { audit: 0.90, test_erosion: 0.85, smoketest: 0.0 },
            smoketest_enabled: false,
            blocker: null,
            audit_missing_reason: null,
            would_promote: false,
            promotion_enabled: false,
            terminal_status: 'not_promoted',
            threshold: 0.9,
            started_at: null,
            finalized_at: '2026-01-01T00:00:00Z',
            created_at: '2026-01-01T00:00:00Z',
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
      expect(history).toBeInTheDocument();
      // The compact sub-score should show "s:—" not "s:0" when smoke is disabled.
      expect(history.textContent).toContain('s:—');
      expect(history.textContent).not.toContain('s:0');
    });
  });

  describe('verdict-fetch error handling', () => {
    it('shows verdicts-error when fetchQualificationVerdicts rejects', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockRejectedValue(new Error('Network error'));

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse()}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      const errorEl = await screen.findByTestId('verdicts-error');
      expect(errorEl).toHaveTextContent('Failed to load verdict history.');
      expect(screen.queryByTestId('verdict-history')).not.toBeInTheDocument();
    });

    it('re-fetches verdicts when data prop changes (refresh cycle)', async () => {
      const { rerender } = render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse()}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(vi.mocked(api.fetchQualificationVerdicts)).toHaveBeenCalledTimes(1);

      const secondData = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({ phase: 'stabilize' }),
      });

      rerender(
        <VersionLifecyclePanel
          data={secondData}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      await waitFor(() => {
        expect(vi.mocked(api.fetchQualificationVerdicts)).toHaveBeenCalledTimes(2);
      });
    });
  });

  describe('attest criterion', () => {
    const TASKS: api.ChangelogEntry[] = [
      { task_type: 'create', title: 'Add feature', pr_number: 1, pr_url: null },
    ];

    function makeStuckCoverage(stuckReason: 'no_mapping' | 'direction_retired'): api.VersionCriterionCoverage {
      return {
        total: 1,
        covered: 0,
        partial: 0,
        uncovered: 1,
        stuck_count: 1,
        criteria: [
          {
            criterion_key: 'k-stuck',
            text: 'Stuck criterion text',
            coverage: 'uncovered',
            evidence_count: 0,
            stuck_reason: stuckReason,
          },
        ],
      };
    }

    it('shows attestation reason dialog and calls attestAcceptanceCriterion on confirm', async () => {
      vi.mocked(api.attestAcceptanceCriterion).mockResolvedValue({ status: 'ok', criterion_key: 'k-stuck', coverage: 'covered' });
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
          criterion_coverage: makeStuckCoverage('no_mapping'),
        }),
      });

      render(<VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />);
      fireEvent.click(screen.getByTestId('changelog-toggle'));

      const attestBtn = await screen.findByTestId('attest-btn-k-stuck');
      fireEvent.click(attestBtn);

      expect(await screen.findByText('Attest criterion')).toBeInTheDocument();
      const input = screen.getByPlaceholderText(/verified live by hand|direction retired|attestation/i) ||
        screen.getByRole('textbox');
      fireEvent.change(input, { target: { value: 'Direction retired; criterion superseded' } });
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

      await waitFor(() => {
        expect(api.attestAcceptanceCriterion).toHaveBeenCalledWith('proj-1', 'k-stuck', 'Direction retired; criterion superseded');
      });
      expect(mockRefresh).toHaveBeenCalled();
    });

    it('does not call API when attestation dialog is cancelled', async () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
          criterion_coverage: makeStuckCoverage('direction_retired'),
        }),
      });

      render(<VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />);
      fireEvent.click(screen.getByTestId('changelog-toggle'));

      const attestBtn = await screen.findByTestId('attest-btn-k-stuck');
      fireEvent.click(attestBtn);

      expect(await screen.findByText('Attest criterion')).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));

      await waitFor(() => {
        expect(screen.queryByText('Attest criterion')).not.toBeInTheDocument();
      });
      expect(api.attestAcceptanceCriterion).not.toHaveBeenCalled();
    });

    it('shows error message when attest API fails', async () => {
      vi.mocked(api.attestAcceptanceCriterion).mockRejectedValue(new Error('Attestation failed'));
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
          criterion_coverage: makeStuckCoverage('no_mapping'),
        }),
      });

      render(<VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />);
      fireEvent.click(screen.getByTestId('changelog-toggle'));

      const attestBtn = await screen.findByTestId('attest-btn-k-stuck');
      fireEvent.click(attestBtn);

      const input = await screen.findByRole('textbox');
      fireEvent.change(input, { target: { value: 'some reason' } });
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

      await waitFor(() => {
        expect(screen.getByText('Attestation failed')).toBeInTheDocument();
      });
    });

    it('Attest and Link direction buttons only appear on stuck active criteria', async () => {
      const coverage: api.VersionCriterionCoverage = {
        total: 2,
        covered: 1,
        partial: 0,
        uncovered: 1,
        stuck_count: 1,
        criteria: [
          {
            criterion_key: 'k-stuck',
            text: 'Stuck criterion',
            coverage: 'uncovered',
            evidence_count: 0,
            stuck_reason: 'no_mapping',
          },
          {
            criterion_key: 'k-ok',
            text: 'Covered criterion',
            coverage: 'covered',
            evidence_count: 1,
          },
        ],
      };
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 2,
          criterion_coverage: coverage,
        }),
      });

      render(<VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />);
      fireEvent.click(screen.getByTestId('changelog-toggle'));

      await screen.findByTestId('criterion-coverage-panel');
      expect(screen.getByTestId('attest-btn-k-stuck')).toBeInTheDocument();
      expect(screen.getByTestId('link-direction-btn-k-stuck')).toBeInTheDocument();
      expect(screen.queryByTestId('attest-btn-k-ok')).not.toBeInTheDocument();
      expect(screen.queryByTestId('link-direction-btn-k-ok')).not.toBeInTheDocument();
    });
  });

  describe('link direction', () => {
    const TASKS: api.ChangelogEntry[] = [
      { task_type: 'create', title: 'Add feature', pr_number: 1, pr_url: null },
    ];
    const stuckCoverage: api.VersionCriterionCoverage = {
      total: 1,
      covered: 0,
      partial: 0,
      uncovered: 1,
      stuck_count: 1,
      criteria: [
        {
          criterion_key: 'k-stuck',
          text: 'Needs direction link',
          coverage: 'uncovered',
          evidence_count: 0,
          stuck_reason: 'direction_retired',
        },
      ],
    };

    it('shows direction-key input dialog and calls linkCriteriaToDirection on confirm', async () => {
      vi.mocked(api.linkCriteriaToDirection).mockResolvedValue({ status: 'ok', direction_key: 'd-abc123', criterion_keys: ['k-stuck'] });
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
          criterion_coverage: stuckCoverage,
        }),
      });

      render(<VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />);
      fireEvent.click(screen.getByTestId('changelog-toggle'));

      const linkBtn = await screen.findByTestId('link-direction-btn-k-stuck');
      fireEvent.click(linkBtn);

      expect(await screen.findByText('Link criterion to direction')).toBeInTheDocument();
      const input = screen.getByRole('textbox');
      fireEvent.change(input, { target: { value: 'd-abc123' } });
      fireEvent.click(screen.getByTestId('confirm-dialog-confirm'));

      await waitFor(() => {
        expect(api.linkCriteriaToDirection).toHaveBeenCalledWith('proj-1', 'd-abc123', ['k-stuck']);
      });
      expect(mockRefresh).toHaveBeenCalled();
    });

    it('does not call API when link direction dialog is cancelled', async () => {
      const data = makeVersionLifecycleResponse({
        current_version: makeCurrentVersion({
          tasks_merged: TASKS,
          merged_task_count: 1,
          criterion_coverage: stuckCoverage,
        }),
      });

      render(<VersionLifecyclePanel data={data} loading={false} projectId="proj-1" onRefresh={mockRefresh} />);
      fireEvent.click(screen.getByTestId('changelog-toggle'));

      const linkBtn = await screen.findByTestId('link-direction-btn-k-stuck');
      fireEvent.click(linkBtn);

      expect(await screen.findByText('Link criterion to direction')).toBeInTheDocument();
      fireEvent.click(screen.getByTestId('confirm-dialog-cancel'));

      await waitFor(() => {
        expect(screen.queryByText('Link criterion to direction')).not.toBeInTheDocument();
      });
      expect(api.linkCriteriaToDirection).not.toHaveBeenCalled();
    });
  });

  describe('completion-debt lane in qualification sub-scores', () => {
    function makeQualWithDebt(debtOverrides: Partial<api.CompletionDebtQualSnapshot> = {}, debtPressure = 0.8): api.VersionQualification {
      return makeQualification({
        sub_scores: {
          audit: 0.9,
          test_erosion: 0.85,
          smoketest: 0.95,
          debt_pressure: debtPressure,
        },
        completion_debt: {
          total_open: 2,
          items: [
            { id: 'd-1', kind: 'default_off_merge', title: 'Feature flag X', description: 'desc', created_at: '2026-01-01T00:00:00Z', age_days: 10 },
            { id: 'd-2', kind: 'observe_mode', title: 'Observe knob Y', description: 'desc', created_at: '2026-01-02T00:00:00Z', age_days: 5 },
          ],
          ...debtOverrides,
        },
      });
    }

    it('renders debt lane and items when debt_pressure and completion_debt are present', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ last_qualification: makeQualWithDebt() }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByText(/debt: 2 open/)).toBeInTheDocument();
      expect(screen.getByTestId('debt-items-list')).toBeInTheDocument();
      expect(screen.getByText('Feature flag X')).toBeInTheDocument();
      expect(screen.getByText('Observe knob Y')).toBeInTheDocument();
    });

    it('omits debt lane when debt_pressure is absent (legacy row)', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ last_qualification: makeQualification({
            sub_scores: { audit: 0.9, test_erosion: 0.85, smoketest: 0.95 },
          }) }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.queryByText(/debt:/)).not.toBeInTheDocument();
      expect(screen.queryByTestId('debt-items-list')).not.toBeInTheDocument();
    });

    it('omits debt lane when last_qualification is null', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ last_qualification: null }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.queryByText(/debt:/)).not.toBeInTheDocument();
    });

    it('renders clean zero-debt state when total_open is 0', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ last_qualification: makeQualification({
            sub_scores: { audit: 0.9, test_erosion: 0.85, smoketest: 0.95, debt_pressure: 1.0 },
            completion_debt: { total_open: 0, items: [] },
          }) }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByText('debt: none')).toBeInTheDocument();
      expect(screen.queryByTestId('debt-items-list')).not.toBeInTheDocument();
    });

    it('shows truncation note when total_open exceeds items length', () => {
      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ last_qualification: makeQualWithDebt({ total_open: 5 }) }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      expect(screen.getByText(/showing 2 oldest/)).toBeInTheDocument();
    });

    it('renders debt segment in verdict history row when debt_pressure present', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [{
          id: 'v-1',
          release_version_id: 'r-1',
          candidate_sha: 'abc1234',
          confidence: 0.85,
          sub_scores: { audit: 0.9, test_erosion: 0.85, smoketest: 0.95, debt_pressure: 0.8 },
          completion_debt: { total_open: 3, items: [] },
          blocker: null,
          audit_missing_reason: null,
          would_promote: false,
          promotion_enabled: false,
          terminal_status: 'not_promoted',
          threshold: 0.9,
          started_at: null,
          finalized_at: '2026-01-01T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
        }],
        total: 1,
      });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ phase: 'stabilize' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      await waitFor(() => {
        expect(screen.getByTestId('verdict-history')).toBeInTheDocument();
      });
      expect(screen.getByText(/d:3open/)).toBeInTheDocument();
    });

    it('omits debt segment in verdict history when debt_pressure absent', async () => {
      vi.mocked(api.fetchQualificationVerdicts).mockResolvedValue({
        verdicts: [{
          id: 'v-1',
          release_version_id: 'r-1',
          candidate_sha: 'abc1234',
          confidence: 0.85,
          sub_scores: { audit: 0.9, test_erosion: 0.85, smoketest: 0.95 },
          blocker: null,
          audit_missing_reason: null,
          would_promote: false,
          promotion_enabled: false,
          terminal_status: 'not_promoted',
          threshold: 0.9,
          started_at: null,
          finalized_at: '2026-01-01T00:00:00Z',
          created_at: '2026-01-01T00:00:00Z',
        }],
        total: 1,
      });

      render(
        <VersionLifecyclePanel
          data={makeVersionLifecycleResponse({ current_version: makeCurrentVersion({ phase: 'stabilize' }) })}
          loading={false}
          projectId="proj-1"
          onRefresh={mockRefresh}
        />
      );

      await waitFor(() => {
        expect(screen.getByTestId('verdict-history')).toBeInTheDocument();
      });
      expect(screen.queryByText(/d:\d+open/)).not.toBeInTheDocument();
    });
  });
});
