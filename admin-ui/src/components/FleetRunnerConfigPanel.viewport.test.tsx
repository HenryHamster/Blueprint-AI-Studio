import { afterEach, beforeEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FleetRunnerConfigPanel } from './FleetRunnerConfigPanel';
import { expectNoOverflowAt } from '../test/viewport';
import type { FleetRunner } from '../api';

// Minimal viewport coverage for the runner Configure panel. This component was
// landed (Configure-panel rebuild) without a viewport test, tripping the
// repo-wide viewport-coverage invariant. A GPU runner exercises the widest
// form (max-concurrent, task-type allowlist, GPU allowlist, GPUs/job) so the
// no-overflow check covers the busiest layout.
function makeRunner(overrides: Partial<FleetRunner> = {}): FleetRunner {
  return {
    id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    hostname: 'gpu-box',
    version: '1.0',
    status: 'active',
    capabilities: {
      task_types: 'all',
      max_concurrent: 2,
      has_gpu: true,
      gpu_count: 2,
    },
    config: {
      desired: {
        max_concurrent: 2,
        task_types: 'all',
        gpu_indices_allowed: 'all',
        gpus_per_job: 1,
      },
      config_generation: 'gen-1',
    },
    last_seen_at: '2026-07-08T10:00:00',
    created_at: '2026-07-01T10:00:00',
    ...overrides,
  };
}

describe('FleetRunnerConfigPanel viewport', () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    container = document.createElement('div');
    container.style.padding = '12px';
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    document.body.removeChild(container);
  });

  test('renders without overflow at 375px (mobile)', async () => {
    await expectNoOverflowAt(375, async () => {
      act(() => {
        root.render(
          <FleetRunnerConfigPanel projectId="p-1" runner={makeRunner()} onSaved={() => {}} />,
        );
      });
    });
  });

  test('renders without overflow at 768px (tablet)', async () => {
    await expectNoOverflowAt(768, async () => {
      act(() => {
        root.render(
          <FleetRunnerConfigPanel projectId="p-1" runner={makeRunner()} onSaved={() => {}} />,
        );
      });
    });
  });

  test('renders without overflow at 1024px (desktop)', async () => {
    await expectNoOverflowAt(1024, async () => {
      act(() => {
        root.render(
          <FleetRunnerConfigPanel projectId="p-1" runner={makeRunner()} onSaved={() => {}} />,
        );
      });
    });
  });
});
