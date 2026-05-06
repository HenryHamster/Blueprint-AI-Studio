import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ManualInterventionPanel } from './PipelineRegressionsPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) =>
    url.includes('/manual-intervention')
      ? new Response(JSON.stringify({ cases: Array.from({ length: 6 }, (_, i) => ({
          id: `case-${i + 1}`, regression_class: `very_long_regression_class_name_${i + 1}_with_nested_failure_signals`,
          signal_name: `signal-${i + 1}`, status: 'NEEDS_ATTENTION', attempts: i + 2, last_remediation_pr_number: 1200 + i,
          last_failure_class: `LONG_FAILURE_CLASS_NAME_${i + 1}_WITH_MULTIPLE_SEGMENTS`,
          signal_snapshot_at_spawn: { task_type: 'create', severity: 'critical', queue_depth: 18 + i, note: 'initial snapshot has enough detail to pressure the card layout' },
          signal_snapshot_at_merge: { task_type: 'create', severity: 'critical', queue_depth: 24 + i, note: 'merge snapshot stays dense enough to exercise wrapping' },
          fingerprint_to_fix_log: [{ fix_pr_number: 700 + i, merged_at: '2026-05-06T00:00:00Z', recovered_at: i % 2 === 0 ? '2026-05-06T01:00:00Z' : undefined }],
          last_seen_at: '2026-05-06T02:00:00Z', needs_manual_intervention_at: '2026-05-06T03:00:00Z',
          first_seen_at: '2026-05-06T01:05:00Z', signal_first_crossed_at: '2026-05-06T01:00:00Z',
        })) }), { status: 200 })
      : new Response('{}', { status: 200 })
  ),
}));

describe('PipelineRegressionsPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) act(() => root?.unmount()); root = null; host?.remove(); host = null; });
  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<ManualInterventionPanel projectId="project-123" />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});
