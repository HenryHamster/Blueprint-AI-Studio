import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TaskCard } from './TaskCard';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';
import type { Task } from '../types/task';

vi.mock('../api', () => ({
  authGet: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
}));

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-viewport-1',
    title: 'feat(auth): implement token-refresh flow with sliding expiry and cross-tab sync for long-running operator sessions',
    description: 'fix(backend): handle edge case where pipeline_regression_dispatcher emits duplicate escalation_reason labels on retry',
    status: 'IN_PROGRESS',
    task_type: 'create',
    created_by: 'blueprint-agent',
    requires_approval: false,
    created_at: new Date(Date.now() - 3600_000).toISOString(),
    started_at: new Date(Date.now() - 1800_000).toISOString(),
    last_heartbeat: new Date(Date.now() - 30_000).toISOString(),
    latest_status_message: 'Running tests — backend/testing/test_pipeline_regression_dispatcher.py passing, frontend build in progress',
    ...overrides,
  };
}

const noop = () => {};
const defaultProps = {
  readOnly: false,
  onApprove: noop,
  onRequestChanges: noop,
  onCancel: noop,
  onRetry: noop,
  onEditPrompt: noop,
  onDismiss: noop,
  onArchive: noop,
  onViewLogs: noop,
};

describe('TaskCard — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<TaskCard task={makeTask()} {...defaultProps} />);
      });
    });
  });

  test('long title and status message do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskCard
            task={makeTask({
              title: 'feat(regression): classify viewport-test failures as a distinct regression class with per-layer escalation_reason metadata attached to each pipeline case',
              latest_status_message: 'backend/testing/test_pipeline_regression_dispatcher.py — 47 tests passing; admin-ui/src/components/PipelineRegressionsPanel.viewport.test.tsx — building',
            })}
            {...defaultProps}
          />
        );
      });
    });
  });

  test('renders FAILED task without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskCard
            task={makeTask({
              status: 'FAILED',
              failure_class: 'CODE_ERROR',
              latest_status_message: 'Re-evaluation limit reached (2 attempts) — children consistently failing on backend migration conflict',
            })}
            {...defaultProps}
          />
        );
      });
    });
  });

  test('FAILED task long status message does not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskCard
            task={makeTask({
              status: 'FAILED',
              failure_class: 'CODE_ERROR',
              latest_status_message: 'Re-evaluation limit reached (2 attempts) — children consistently failing on backend/alembic/versions/0042_add_escalation_reason.py migration conflict with existing revision chain',
            })}
            {...defaultProps}
          />
        );
      });
    });
  });

  test('action-toolbar buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskCard
            task={makeTask({
              status: 'EVALUATING',
              requires_approval: true,
              attempt_count: 2,
            })}
            {...defaultProps}
            maxRetries={5}
            blockingCount={0}
          />
        );
      });
    }, { selector: 'button[title]' });
  });

  test('verify_tests task renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskCard
            task={makeTask({
              status: 'COMPLETED',
              task_type: 'verify_tests',
              title: 'Verify tests on PR head before the no-CI merge gate decides whether to merge',
            })}
            {...defaultProps}
          />
        );
      });
    });
  });

  test('FAILED task with SR badge and direction button meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <TaskCard
            task={makeTask({
              status: 'FAILED',
              failure_class: 'CODE_ERROR',
              context: {
                direction_id: 'd-e337d3b2',
                _self_review_outcome: {
                  verdict: 'pass',
                  fixes_applied: false,
                  check_results: { correctness: { passed: true } },
                },
              },
            })}
            {...defaultProps}
            onTrack={noop}
            onDirectionClick={noop}
          />
        );
      });
    }, { selector: 'button' });
  });
});
