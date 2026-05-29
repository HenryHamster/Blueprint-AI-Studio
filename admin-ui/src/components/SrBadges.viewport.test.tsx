import { afterEach, beforeEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SrBadge, SrDisallowedBadge, SrDivergenceBadge } from './SrBadges';
import type { Task } from '../types/task';
import { expectNoOverflowAt } from '../test/viewport';

function makeTask(context: Record<string, unknown>): Task {
  return {
    id: 'task-1',
    title: 'Test Task',
    description: 'desc',
    status: 'COMPLETED',
    task_type: 'create',
    created_at: '2024-01-01T00:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    context,
  } as Task;
}

// One task per badge variant so every visual branch is rendered at 375px.
const PASS_TASK = makeTask({
  _self_review_outcome: { verdict: 'pass', fixes_applied: false, check_results: { correctness: { passed: true } } },
});
const PASS_FIXED_TASK = makeTask({
  _self_review_outcome: { verdict: 'pass', fixes_applied: true, check_results: { correctness: { passed: false } } },
});
const FAIL_TASK = makeTask({
  _self_review_outcome: { verdict: 'fail', check_results: { correctness: { passed: false }, security: { passed: false } } },
});
const SKIP_TASK = makeTask({
  _self_review_outcome: { verdict: 'unknown', check_results: {}, skip_reason: 'reviewer_session_failed' },
});
const MISS_TASK = makeTask({
  _self_review_outcome: { verdict: 'pass', check_results: { correctness: { passed: true } } },
  _first_attempt_rejection_categories: { security: {}, performance: {} },
});
const DISALLOWED_TASK = makeTask({
  _self_review_outcome: { verdict: 'pass', check_results: { correctness: { passed: true } }, _filtered_blind_spots_count: 3 },
});
const DIVERGENCE_TASK = makeTask({
  _self_review_outcome: { verdict: 'pass', check_results: { correctness: { passed: false } } },
  _first_attempt_rejection_categories: { security: {} },
});

describe('SrBadges — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders all SR badge variants without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.width = '375px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <div className="flex flex-wrap gap-1">
            <SrBadge task={PASS_TASK} />
            <SrBadge task={PASS_TASK} onClick={() => {}} />
            <SrBadge task={PASS_FIXED_TASK} />
            <SrBadge task={FAIL_TASK} />
            <SrBadge task={SKIP_TASK} />
            <SrBadge task={MISS_TASK} />
            <SrDisallowedBadge task={DISALLOWED_TASK} />
            <SrDivergenceBadge task={DIVERGENCE_TASK} />
            <SrDivergenceBadge task={DIVERGENCE_TASK} onClick={() => {}} />
          </div>,
        );
      });
    });
  });
});
