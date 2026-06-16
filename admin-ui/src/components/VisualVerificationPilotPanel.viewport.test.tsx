import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { VisualVerificationPilotPanel } from './VisualVerificationPilotPanel';
import type { Project } from '../types/project';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  API_BASE_URL: 'http://localhost',
  fetchVisualPilotStatus: vi.fn(),
  resolveViewportShotDownloadUrl: vi.fn().mockResolvedValue('https://example.com/shot.png'),
}));

import * as api from '../api';

const BASE_PROJECT: Project = {
  id: 'proj-self-1',
  name: 'BlueprintAIStudio',
  status: 'active',
  github_owner: 'HenryHamster',
  github_repo: 'BlueprintAIStudio',
  autonomous_enabled: true,
  created_at: '2026-01-01T00:00:00Z',
  is_self_repo: true,
};

const THRESHOLDS = {
  target_admin_tasks: 25,
  target_days: 42,
  early_pause_infra_rate: 0.3,
};

const STATUS_WITH_CAUGHT = {
  project_id: 'proj-self-1',
  admin_task_count: 20,
  entry_count: 8,
  malformed_count: 0,
  pairs_inspected: 34,
  pairs_changed: 12,
  infra_total: 12,
  infra_rate: 0.35,
  days_elapsed: 10,
  caught_count: 2,
  caught: [
    {
      task_id: 'task-abc-123',
      task_title: 'feat: add VisualVerificationPilotPanel operator surface',
      note: 'Button label clipped on mobile — caught before merge',
      created_at: '2026-06-10T12:00:00Z',
      viewport_shot_names: ['main-before', 'main-after', 'main-diff'],
      viewport_shot_entries: [
        { name: 'main-before' },
        { name: 'main-after', magnitude: 0.23 },
        { name: 'main-diff' },
      ],
    },
    {
      task_id: 'task-def-456',
      task_title: 'fix: ImprovementLanesPanel overflow at 375px on narrow viewports',
      note: null,
      created_at: '2026-06-11T14:00:00Z',
      viewport_shot_names: [],
      viewport_shot_entries: [],
    },
  ],
  thresholds: THRESHOLDS,
  generated_at: '2026-06-13T00:00:00Z',
};

const STATUS_RUNNING_NO_CATCHES = {
  project_id: 'proj-self-1',
  admin_task_count: 15,
  entry_count: 5,
  malformed_count: 0,
  pairs_inspected: 20,
  pairs_changed: 8,
  infra_total: 2,
  infra_rate: 0.1,
  days_elapsed: 7,
  caught_count: 0,
  caught: [],
  thresholds: THRESHOLDS,
  generated_at: '2026-06-13T00:00:00Z',
};

const STATUS_EMPTY = {
  project_id: 'proj-self-1',
  admin_task_count: 0,
  entry_count: 0,
  malformed_count: 0,
  pairs_inspected: 0,
  pairs_changed: 0,
  infra_total: 0,
  infra_rate: 0,
  days_elapsed: 2,
  caught_count: 0,
  caught: [],
  thresholds: THRESHOLDS,
  generated_at: '2026-06-13T00:00:00Z',
};

describe('VisualVerificationPilotPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    vi.restoreAllMocks();
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  async function renderWith(
    statusData: typeof STATUS_WITH_CAUGHT,
    onNavigateToTask?: (taskId: string) => void,
  ) {
    vi.mocked(api.fetchVisualPilotStatus).mockResolvedValue(statusData);
    host = document.createElement('div');
    host.style.padding = '16px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <VisualVerificationPilotPanel
          project={BASE_PROJECT}
          onNavigateToTask={onNavigateToTask}
        />,
      );
    });
    // Allow the async fetch effect to resolve.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    // Re-render after state update settles.
    await act(async () => {});
  }

  test('renders caught entries with EARLY PAUSE RISK at 375px without overflow', async () => {
    await expectNoOverflowAt(375, () => renderWith(STATUS_WITH_CAUGHT));
  });

  test('renders running state with no catches at 375px without overflow', async () => {
    await expectNoOverflowAt(375, () => renderWith(STATUS_RUNNING_NO_CATCHES));
  });

  test('renders empty state at 375px without overflow', async () => {
    await expectNoOverflowAt(375, () => renderWith(STATUS_EMPTY));
  });

  test('renders NOT STARTED badge when entry_count and admin_task_count are zero', async () => {
    await renderWith(STATUS_EMPTY);
    const badgeByText = Array.from(host!.querySelectorAll('span')).find(
      (el) => el.textContent === 'NOT STARTED',
    );
    if (!badgeByText) {
      throw new Error('Expected a "NOT STARTED" badge but none was found');
    }
  });

  test('renders caught entries at 1024px without overflow', async () => {
    await expectNoOverflowAt(1024, () => renderWith(STATUS_WITH_CAUGHT));
  });

  test('renders running state at 1024px without overflow', async () => {
    await expectNoOverflowAt(1024, () => renderWith(STATUS_RUNNING_NO_CATCHES));
  });

  test('renders expanded screenshots for caught entry at 375px without overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      await renderWith(STATUS_WITH_CAUGHT);
      // Click the "View screenshots" toggle on the first caught entry.
      const toggles = Array.from(host!.querySelectorAll('button')).filter(
        (el) => el.textContent?.includes('View') && el.textContent?.includes('screenshot'),
      );
      if (toggles.length === 0) {
        throw new Error('Expected a "View screenshots" toggle button but none was found');
      }
      await act(async () => {
        (toggles[0] as HTMLButtonElement).click();
      });
    });
  });

  test('renders expanded screenshots for caught entry at 1024px without overflow', async () => {
    await expectNoOverflowAt(1024, async () => {
      await renderWith(STATUS_WITH_CAUGHT);
      const toggles = Array.from(host!.querySelectorAll('button')).filter(
        (el) => el.textContent?.includes('View') && el.textContent?.includes('screenshot'),
      );
      if (toggles.length === 0) {
        throw new Error('Expected a "View screenshots" toggle button but none was found');
      }
      await act(async () => {
        (toggles[0] as HTMLButtonElement).click();
      });
    });
  });

  test('renders magnitude badge when viewport_shot_entries carries magnitude at 375px without overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      await renderWith(STATUS_WITH_CAUGHT);
      // Expand screenshots for first caught entry.
      const toggles = Array.from(host!.querySelectorAll('button')).filter(
        (el) => el.textContent?.includes('View') && el.textContent?.includes('screenshot'),
      );
      if (toggles.length === 0) throw new Error('Expected a "View screenshots" toggle button');
      await act(async () => { (toggles[0] as HTMLButtonElement).click(); });
      // Magnitude badge should be visible (23.0% changed).
      const badge = Array.from(host!.querySelectorAll('span')).find(
        (el) => el.textContent?.includes('% changed'),
      );
      if (!badge) throw new Error('Expected a magnitude badge (e.g. "23.0% changed") but none was found');
    });
  });
});
