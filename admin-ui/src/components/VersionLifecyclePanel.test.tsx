import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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
        expect(cutVersionMock).toHaveBeenCalledWith('proj-1', undefined);
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
        expect(cutVersionMock).toHaveBeenCalledWith('proj-1', 'stalled in build');
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
});
