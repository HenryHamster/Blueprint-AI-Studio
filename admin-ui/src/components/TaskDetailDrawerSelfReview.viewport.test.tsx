import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SelfReviewPanel } from './TaskDetailDrawerSelfReview';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const apiFetchMock = vi.fn<(...args: unknown[]) => Promise<Response>>(async () => new Response(JSON.stringify({}), { status: 200 }));

vi.mock('../api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

const richTask: Task = {
  id: 'task-550e8400e29b41d4a716446655440000',
  title: 'Test Task with Self-Review and Long Identifiers',
  description: 'A test description',
  status: 'COMPLETED',
  task_type: 'create',
  created_at: '2024-01-01T00:00:00Z',
  created_by: 'USER',
  requires_approval: false,
  context: {
    _self_review_outcome: {
      verdict: 'fail',
      fixes_applied: true,
      check_results: {
        'correctness': { passed: false, issue: 'Off-by-one in loop boundary calculation near line 42', reasoning: 'Verified that the loop condition at line 42 does not include the upper bound, which causes the last element to be skipped.' },
        'type-safety': { passed: false, issue: 'Missing null check on optional field access', reasoning: 'ok' },
        'scope-drift': { passed: true, reasoning: 'All changes are contained within the declared scope boundaries.' },
        'style-conventions': { passed: true, reasoning: 'Linting passed with no violations.' },
      },
      blind_spots: ['Did not re-read the serialization path after the diff was truncated'],
      edges_considered: [
        { case: 'When the input contains a very long path like /usr/local/lib/python3.11/site-packages/some_deeply_nested_module/utils/helpers.py', handled: true, evidence: 'Path is split at OS separator' },
        { case: 'Edge case where the task context is null or undefined at the point of serialization', handled: false, blind_spot: 'No guard for undefined context fields' },
      ],
      reviewer_findings: [
        { category: 'correctness', severity: 'HIGH', description: 'The loop iterates past the end of the array when the input length is exactly a multiple of the batch size' },
        { category: 'error-handling', severity: 'MEDIUM', description: 'Unhandled promise rejection in the background sync path' },
      ],
    },
    _first_attempt_rejection_categories: {
      'correctness': { note: 'flagged by reviewer' },
      'type-safety': { note: 'missing types' },
      'documentation': {},
    },
  },
};


async function renderExpanded(host: HTMLDivElement, r: Root) {
  await act(async () => { r.render(<SelfReviewPanel task={richTask} />); });
  const buttons = host.querySelectorAll('button');
  const srButton = Array.from(buttons).find(b => b.textContent?.includes('Self-Review'));
  if (srButton) {
    await act(async () => { srButton.click(); });
  }
}

describe('TaskDetailDrawerSelfReview SelfReviewPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    apiFetchMock.mockClear();
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders expanded panel at 375px without horizontal overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderExpanded(host, r);
    });
  });

  test('does not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderExpanded(host, r);
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    // Mirror the prior DetailsTab viewport test: assert touch targets on the
    // collapsed panel. The collapse header opts out via
    // data-viewport-allow-small-target; the inner "Request fresh review"
    // control is pre-existing markup (unchanged by this extraction) and is
    // only revealed when expanded, matching the original gate's scope.
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<SelfReviewPanel task={richTask} />); });
    }, { selector: 'button, a[href]' });
  });

  test('renders verdict, failed + passed checks, reasoning, edges, reviewer findings, and agent-vs-human table', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderExpanded(host, r);

    const content = host.textContent ?? '';
    // verdict
    expect(content).toContain('Verdict');
    // failed + passed checks reasoning
    expect(content).toContain('Verified that the loop condition');
    expect(content).toContain('All changes are contained within');
    expect(content).toContain('Linting passed with no violations');
    // blind spots
    expect(content).toContain('Did not re-read the serialization path');
    // edges considered
    expect(content).toContain('Edges considered');
    expect(content).toContain('No guard for undefined context fields');
    // reviewer findings
    expect(content).toContain('Fresh-reviewer findings');
    expect(content).toContain('Unhandled promise rejection');
    // agent vs human table
    expect(content).toContain('Agent vs. Human review categories');
  });

  test('Request fresh review button fires apiFetch to the request-fresh-reviewer endpoint', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderExpanded(host, r);

    const buttons = host.querySelectorAll('button');
    const freshBtn = Array.from(buttons).find(b => b.textContent?.includes('Request fresh review'));
    expect(freshBtn).toBeTruthy();
    await act(async () => { freshBtn?.click(); });

    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    expect(apiFetchMock.mock.calls[0][0]).toBe(`/api/admin/tasks/${richTask.id}/request-fresh-reviewer`);
    expect(apiFetchMock.mock.calls[0][1]).toEqual({ method: 'POST' });
  });

});
