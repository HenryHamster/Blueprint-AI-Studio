import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

import { CatchUpFailureSignalsSection } from './CatchUpFailureSignalsSection';
import type { DirectionProgress } from './DirectionProgressSection';
import { expectNoOverflowAt } from '../test/viewport';

describe('CatchUpFailureSignalsSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
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

  test('renders all failure-signal variants without overflow at 375px and opens the first task on click', async () => {
    const onSelectTask = vi.fn();

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <CatchUpFailureSignalsSection
            recurringErrorClusters={[
              {
                fingerprint: 'cluster-a',
                error_snippet: 'TimeoutError: agent step exceeded wall-clock limit while hydrating a long operator session and reconciling cross-tab state',
                count: 4,
                direction_ids: ['d-1', 'd-2'],
                task_types: ['review', 'fix', 'research'],
                first_task_id: 'task-cluster-a',
              },
              {
                fingerprint: 'cluster-b',
                error_snippet: 'TypeError: cannot read properties of undefined while normalizing a deeply nested task payload',
                count: 2,
                direction_ids: ['d-3'],
                task_types: ['plan'],
                first_task_id: 'task-cluster-b',
              },
            ]}
            earlyWarnings={[
              {
                failure_class: 'CODE_ERROR',
                rate_progression: [0.08, 0.14, 0.22],
                affected_direction_count: 3,
                severity: 'high',
                suggested_action: 'tighten validation before the next merge window',
              },
              {
                failure_class: 'RATE_LIMIT',
                rate_progression: [0.05, 0.11],
                affected_direction_count: 1,
                severity: 'medium',
                suggested_action: 'back off bursty follow-up requests',
              },
            ]}
            recurringPatterns={[
              {
                fingerprint: 'pattern-a',
                example_message: 'The same remediation sequence kept reappearing across multiple retries after the first fix shipped',
                occurrence_count: 5,
                affected_direction_count: 2,
                remediation_status: 'fix_shipped',
                severity: 'medium',
                proactive: true,
              },
              {
                fingerprint: 'pattern-b',
                example_message: 'A recurring detection-to-fix loop remains unaddressed even after repeated reviewer escalation',
                occurrence_count: 3,
                affected_direction_count: 1,
                remediation_status: 'unaddressed',
                severity: 'high',
              },
            ]}
            directionProgress={[
              { direction_id: 'd-1', direction_text: 'Improve admin UI ergonomics for dense operator workflows with a very long direction name that should truncate' } as DirectionProgress,
              { direction_id: 'd-2', direction_text: 'Harden review and diagnostics paths across shared task surfaces with another long direction label' } as DirectionProgress,
              { direction_id: 'd-3', direction_text: 'Stabilize task payload normalization and error reporting for backend-to-frontend handoffs' } as DirectionProgress,
            ]}
            onSelectTask={onSelectTask}
          />,
        );
      });
      await act(async () => {
        (document.querySelector('[role="button"]') as HTMLElement | null)?.click();
      });
    }, { waitMs: 50 });

    expect(onSelectTask).toHaveBeenCalledWith({ id: 'task-cluster-a' });
  });
});
