import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { RuntimeWorkspace } from './RuntimeWorkspace';
import type { RuntimeSection } from './OperatorWorkspaces';
import { expectMinTouchTargetAt, expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('./ConsoleHome', () => ({
  ConsoleHome: () => <div className="min-h-[220px] rounded-md border border-neutral-800 p-3">health</div>,
}));

vi.mock('./WarmVmPanel', () => ({
  WarmVmPanel: () => <div className="min-h-[260px] text-sm text-neutral-300">VM Fleet</div>,
}));

vi.mock('./FleetRunnersPanel', () => ({
  FleetRunnersPanel: () => <div className="min-h-[260px] text-sm text-neutral-300">Runners</div>,
}));

vi.mock('./ComputeJobsPanel', () => ({
  ComputeJobsPanel: () => <div className="min-h-[320px] rounded-md border border-neutral-800 p-3">Compute jobs</div>,
  CompactJobsTable: () => <div>Compact jobs table</div>,
  default: () => <div className="min-h-[320px] rounded-md border border-neutral-800 p-3">Compute jobs</div>,
}));

vi.mock('./DashboardHome', () => ({
  DashboardHome: () => <div className="min-h-[220px] rounded-md border border-neutral-800 p-3">analytics</div>,
}));

vi.mock('./SettingsSectionRouter', () => ({
  SettingsSectionRouter: ({ activeSection }: { activeSection: string }) => (
    <div className="min-h-[320px] rounded-md border border-neutral-800 p-3" data-testid="settings-section">
      {activeSection}
    </div>
  ),
}));

function runtimeProps(runtimeSection: RuntimeSection) {
  return {
    runtimeSection,
    onRuntimeSectionChange: vi.fn(),
    runtimeConfigSection: 'orchestrator' as const,
    onRuntimeConfigSectionChange: vi.fn(),
    selectedProjectId: 'proj-1',
    selectedProjectRepoLabel: 'org/repo',
    projects: [],
    tasks: [],
    scopedActiveCount: 1,
    scopedQueuedCount: 2,
    globalActiveCount: 3,
    globalQueuedCount: 4,
    userRole: 'admin' as const,
    apiHealthy: true,
    wsConnected: true,
    settingsRouterProps: {} as never,
    addToast: vi.fn(),
    onViewTask: vi.fn(),
    onViewTaskById: vi.fn(),
    onOpenFleet: vi.fn(),
    onCreateTask: vi.fn(),
    onLaunchCompute: vi.fn(),
    onDirectionClick: vi.fn(),
    onConfigureTargets: vi.fn(),
    onSpawnOrphanFix: vi.fn(),
    onPanelReviewPr: vi.fn(),
    onApproveExperiment: vi.fn(async () => {}),
    onDismissExperiment: vi.fn(async () => {}),
    onNavigateToKnowledge: vi.fn(),
    onRestartOrchestrator: vi.fn(async () => {}),
    versionLifecycle: null,
    versionLifecycleError: null,
    versionLifecycleLastSuccessAt: null,
    versionLifecycleRefreshing: false,
    refreshVersionLifecycle: vi.fn(async () => {}),
  };
}

describe('RuntimeWorkspace viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  function cleanupHost() {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
    }
    root = null;
    host?.remove();
    host = null;
  }

  afterEach(() => {
    cleanupHost();
  });

  async function renderRuntime(section: RuntimeSection) {
    cleanupHost();
    host = document.createElement('div');
    host.style.height = '760px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<RuntimeWorkspace {...runtimeProps(section)} />);
    });
  }

  test('devices surface fits mobile width without nested-panel overflow', async () => {
    await expectNoOverflowAt(375, () => renderRuntime('devices'));
  });

  test('accounts surface fits mobile and desktop workspace viewports', async () => {
    await expectNoOverflowAt(375, () => renderRuntime('accounts'));
    await expectNoOverflowAt(1280, () => renderRuntime('accounts'), { height: 760 });
  });

  test('settings surface satisfies required mobile viewport checks', async () => {
    await expectNoTextTruncationAt(375, () => renderRuntime('settings'), {
      height: 760,
      selector: '[aria-label="Runtime settings sections"] *, [data-testid="settings-section"]',
    });
    await expectMinTouchTargetAt(375, () => renderRuntime('settings'), { height: 760 });
  });

  test('settings surface keeps compact navigation on mobile', async () => {
    await expectNoOverflowAt(375, () => renderRuntime('settings'));
    expect(document.querySelector('[aria-label="Runtime settings sections"]')?.textContent).not.toContain('Global slots, retries');
  });

  test('compute surface does not duplicate the child panel header copy', async () => {
    await expectNoOverflowAt(1024, () => renderRuntime('compute'));
    expect(document.body.textContent).not.toContain('Active experiments, held approvals, and terminal run analysis.');
  });
});
