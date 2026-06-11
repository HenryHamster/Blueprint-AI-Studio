import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { PipelineRegressionsPanel } from './PipelineRegressionsPanel';
import { expectFullViewportAt } from '../test/viewport';
import { __pollerInternals } from '../hooks/usePoller';

vi.mock('../api', () => ({
  fetchPipelineRegressions: vi.fn(async () => ({
    project_id: 'proj-1',
    healthy: false,
    regressions: [
      {
        signal_name:
          'SR catch-rate regression: create tasks are now underperforming on long-form prompts with a very long signal name',
        regression_class: 'sr_catch_rate:create_long_form',
        detected_at: '2026-06-08T10:00:00',
        severity: 'warning',
        current_rate: 0.42,
        prior_rate: 0.31,
      },
      {
        signal_name:
          'Fix-loop divergence regression: test loop produces repeated divergence on compact mobile layout conditions',
        regression_class: 'fix_loop_divergence:test_long_form',
        detected_at: '2026-06-08T11:15:00',
        severity: 'critical',
        current_rate: 0.18,
        prior_rate: 0.62,
      },
    ],
  })),
  resolveViewportFailureDownloadUrl: vi.fn(async () => 'https://storage.example/viewport.png'),
}));

describe('PipelineRegressionsPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) {
      act(() => {
        root?.unmount();
      });
    }
    root = null;
    if (host) host.remove();
    host = null;
    __pollerInternals.reset();
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectFullViewportAt(
      375,
      async () => {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        root = createRoot(host);
        await act(async () => {
          root?.render(<PipelineRegressionsPanel projectId="proj-1" />);
        });
        await act(async () => {
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        });
      },
      { waitMs: 50 },
    );
  });
});
