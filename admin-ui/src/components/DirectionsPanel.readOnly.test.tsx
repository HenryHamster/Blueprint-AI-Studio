import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { DirectionsPanel } from './DirectionsPanel';

const spies = vi.hoisted(() => ({
  mutation: vi.fn(async () => {}),
}));

vi.mock('../hooks/useDirectionsData', () => ({
  useDirectionsData: () => ({
    directions: [],
    autonomousEnabled: false,
    loading: false,
    taskCounts: {},
    activeTasks: {},
    designDoc: { overview: 'readable' },
    problemModel: { gaps: [] },
    problemModelSizeHealth: null,
    runningUpdate: 'readable update',
    runningUpdateAckAt: null,
    runningUpdateReconciliation: null,
    selfReflectionLog: [],
    openVersionNumber: null,
    currentVersion: null,
    error: null,
    setError: vi.fn(),
    researchingIds: {},
    researchSuccessIds: {},
    forceConfirmId: null,
    setForceConfirmId: vi.fn(),
    form: {},
    config: { cooldownMinutes: 240 },
    triggerCycle: spies.mutation,
    addDirection: spies.mutation,
    updateDirection: spies.mutation,
    quickToggleStatus: spies.mutation,
    reactivateDirection: spies.mutation,
    activateStagedDirection: spies.mutation,
    retargetStagedDirection: spies.mutation,
    cancelStagedDirection: spies.mutation,
    unholdDirection: spies.mutation,
    markDirectionComplete: spies.mutation,
    approveProposal: spies.mutation,
    rejectProposal: spies.mutation,
    reviveProposal: spies.mutation,
    applyDirectionUpdate: spies.mutation,
    dismissDirectionUpdate: spies.mutation,
    deleteDirection: spies.mutation,
    reorderDirections: spies.mutation,
    triggerResearch: spies.mutation,
    decideChurnRetirement: spies.mutation,
    linkDirectionToGap: spies.mutation,
    setProblemModel: vi.fn(),
    setRunningUpdate: vi.fn(),
    setRunningUpdateAckAt: vi.fn(),
  }),
}));

vi.mock('./DirectionsPanelHeader', () => ({
  DirectionsPanelHeader: ({ showCycleControls }: { showCycleControls?: boolean }) => (
    <div data-testid="directions-header" data-cycle-controls={String(Boolean(showCycleControls))} />
  ),
}));

vi.mock('./DirectionsList', () => ({
  DirectionsList: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="directions-list" data-read-only={String(Boolean(readOnly))} />
  ),
}));

vi.mock('./AutonomousConfigSection', () => ({
  AutonomousConfigSection: () => <div data-testid="autonomous-config" />,
}));

vi.mock('./ProblemModelViewer', () => ({
  default: ({ readOnly, onSpawnDirection, onLinkDirection }: {
    readOnly?: boolean;
    onSpawnDirection?: unknown;
    onLinkDirection?: unknown;
  }) => (
    <div
      data-testid="problem-model"
      data-read-only={String(Boolean(readOnly))}
      data-has-spawn={String(Boolean(onSpawnDirection))}
      data-has-link={String(Boolean(onLinkDirection))}
    />
  ),
}));

vi.mock('./RunningUpdatePanel', () => ({
  default: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="running-update" data-read-only={String(Boolean(readOnly))} />
  ),
}));

describe('DirectionsPanel read-only wiring', () => {
  beforeEach(() => vi.clearAllMocks());

  test('viewer has read projections without direction/config/cycle mutation surfaces', () => {
    const common = {
      addToast: vi.fn(),
      selectedProjectId: 'project-1',
      projects: [],
      readOnly: true,
      canManageGlobalSettings: false,
      showInternalTabs: false,
    };
    const { rerender } = render(<DirectionsPanel {...common} activeTab="directions" />);

    expect(screen.getByTestId('directions-header')).toHaveAttribute('data-cycle-controls', 'false');
    expect(screen.getByTestId('directions-list')).toHaveAttribute('data-read-only', 'true');
    expect(screen.queryByRole('button', { name: 'New direction' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('autonomous-config')).not.toBeInTheDocument();

    rerender(<DirectionsPanel {...common} activeTab="problem-model" />);
    expect(screen.getByTestId('problem-model')).toHaveAttribute('data-read-only', 'true');
    expect(screen.getByTestId('problem-model')).toHaveAttribute('data-has-spawn', 'false');
    expect(screen.getByTestId('problem-model')).toHaveAttribute('data-has-link', 'false');

    rerender(<DirectionsPanel {...common} activeTab="running-update" />);
    expect(screen.getByTestId('running-update')).toHaveAttribute('data-read-only', 'true');
    expect(spies.mutation).not.toHaveBeenCalled();
  });
});
