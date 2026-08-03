import { afterEach, describe, test } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { ProjectResourceAvailabilityCard } from './ProjectResourceAvailabilityCard';
import type { ProjectResourceSummary } from '../hooks/useProjectResourceSummary';
import { expectFullViewportAt, expectNoOverflowAt } from '../test/viewport';

const DENSE_SUMMARY: ProjectResourceSummary = {
  project_id: 'project-resource-viewport',
  policy: {
    execution_resources: 'project owner',
    attribution: 'launcher',
    managed_by: 'owner',
  },
  accounts: {
    total: 128,
    enabled: 96,
    enabled_below_cap: 73,
    at_cap: 23,
    project_active_tasks: 48,
    project_daily_task_count: 12345,
    project_daily_task_cap: 100000,
    project_max_concurrent_tasks: null,
  },
  runners: { total: 48, online: 31, offline: 17 },
};

describe('ProjectResourceAvailabilityCard — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const mountedRoot = root;
    if (mountedRoot) act(() => mountedRoot.unmount());
    root = null;
    host?.remove();
    host = null;
  });

  const mount = async (
    props: Parameters<typeof ProjectResourceAvailabilityCard>[0],
  ) => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(<ProjectResourceAvailabilityCard {...props} />);
    });
  };

  test('dense aggregate values and policy copy remain readable at 375px', async () => {
    await expectFullViewportAt(375, () => mount({
      data: DENSE_SUMMARY,
      loading: false,
      error: null,
    }));
  });

  test('loading state remains bounded at 375px', async () => {
    await expectNoOverflowAt(375, () => mount({
      data: null,
      loading: true,
      error: null,
    }));
  });

  test('generic unavailable state remains bounded at 375px', async () => {
    await expectNoOverflowAt(375, () => mount({
      data: null,
      loading: false,
      error: 'sensitive provider detail must not be rendered',
    }));
  });
});
