import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DetailsTab } from './TaskDetailDrawerMeta';
import type { Task } from '../types/task';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

vi.mock('../api', () => ({
  authGet: vi.fn(async () => new Response(JSON.stringify({ stats: {} }), { status: 200 })),
  apiFetch: vi.fn(async () => new Response(JSON.stringify({ task_id: 'task-1', fix_loop_history: {}, summary: {} }), { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
  fetchTaskSerialConsole: vi.fn(async () => ({ serial_log: '', source: 'cached', vm_status: 'TERMINATED', vm_instance_id: 'test' })),
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
  parent_task_id: 'task-550e8400e29b41d4a716446655440001',
  project_id: '550e8400-e29b-41d4-a716-446655440000',
  vm_instance_id: 'blueprint-agent-c99e9c2f7a9e8b1c2d3e4f5a6b7c8d9e-0123',
  branch: 'feature/add-comprehensive-error-handling-to-data-pipeline',
  repo_url: 'https://github.com/HenryHamster/BlueprintAIStudio',
  pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/4829',
  commit_hash: '550e8400e29b41d4a716446655440000abcdef12',
  child_task_ids: ['task-550e8400e29b41d4a716446655440002', 'task-550e8400e29b41d4a716446655440003'],
  artifacts: [
    {
      artifact_type: 'PULL_REQUEST',
      pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/4829',
      iteration: 1,
    },
    {
      artifact_type: 'GIT_COMMIT',
      commit_hash: '550e8400e29b41d4a716446655440000abcdef12',
      repo_url: 'https://github.com/HenryHamster/BlueprintAIStudio',
      iteration: 1,
    },
  ],
  payloads: [
    {
      type: 'create',
      branch: 'feature/add-comprehensive-error-handling-to-data-pipeline-processing',
      base_branch: 'main',
      pr_number: 4829,
    },
  ],
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

const memoTask: Task = {
  id: 'task-memo-550e8400e29b41d4a716446655440000',
  title: 'Memo: explain cache strategy',
  description: 'Document reasoning about cache strategy',
  status: 'COMPLETED',
  task_type: 'memo',
  created_at: '2024-01-01T00:00:00Z',
  created_by: 'ORCHESTRATOR',
  requires_approval: false,
  context: {
    research_task_id: 'task-research-550e8400e29b41d4a716446655440001',
    finding_index: 2,
  },
  plan_output: {
    memo: {
      summary: 'Cache strategy: LRU with TTL provides optimal balance between memory and hit rate',
      reasoning: 'After analyzing the workload patterns, LRU eviction with a 1-hour TTL achieves:\n• 92% cache hit rate under normal load\n• Predictable memory usage bounded at 512MB\n• Sub-100ms p99 latency for cache operations\n\nAlternatives like W-TinyLFU add complexity without measurable benefit here.',
      citations: ['task:task-550e8400e29b41d4a716446655440005', 'direction:d-cache-analysis-1234'],
      tags: ['optimization', 'performance', 'caching'],
    },
  },
};

const llmTask: Task = {
  ...richTask,
  id: 'task-550e8400e29b41d4a716446655440004',
  context: {
    ...richTask.context,
    llm_provider: 'openai',
    llm_model: 'gpt-5-mini',
  },
};

const mergeTestTask: Task = {
  id: 'task-verify-550e8400e29b41d4a716446655440099',
  title: 'Verify tests on PR head',
  description: 'Run the repo detected suite as the no-CI merge gate',
  status: 'COMPLETED',
  task_type: 'verify_tests',
  created_at: '2024-01-01T00:00:00Z',
  created_by: 'ORCHESTRATOR',
  requires_approval: false,
  artifacts: [
    {
      artifact_type: 'MERGE_TEST_RESULT',
      logs: JSON.stringify({
        detected: true,
        passed: false,
        tests: [{ name: 'jest', status: 'fail', message: 'test failures detected' }],
        output: 'FAIL src/components/SomeDeeplyNestedComponentWithAVeryLongName.test.tsx — expected 2 received 3 at assertion line 187',
      }),
      iteration: 1,
    },
  ],
};

describe('TaskDetailDrawerMeta DetailsTab — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders DetailsTab with SelfReviewPanel expanded at 375px without horizontal overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={richTask} />); });

      // Expand the Self-Review panel
      const buttons = host.querySelectorAll('button');
      const srButton = Array.from(buttons).find(b => b.textContent?.includes('Self-Review'));
      if (srButton) {
        await act(async () => { srButton.click(); });
      }
    });
  });

  test('displays reasoning text for both passed and failed checks', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => { r.render(<DetailsTab task={richTask} />); });

    // Expand the Self-Review panel
    const buttons = host.querySelectorAll('button');
    const srButton = Array.from(buttons).find(b => b.textContent?.includes('Self-Review'));
    if (srButton) {
      await act(async () => { srButton.click(); });
    }

    // Check that reasoning text is rendered for failed checks
    const content = host.textContent ?? '';
    expect(content).toContain('Verified that the loop condition');
    expect(content).toContain('ok');

    // Check that reasoning text is rendered for passed checks
    expect(content).toContain('All changes are contained within');
    expect(content).toContain('Linting passed with no violations');
  });

  test('meta key/value pairs do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={richTask} />); });

      // Expand the Self-Review panel to show all meta information
      const buttons = host.querySelectorAll('button');
      const srButton = Array.from(buttons).find(b => b.textContent?.includes('Self-Review'));
      if (srButton) {
        await act(async () => { srButton.click(); });
      }
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={richTask} />); });
    }, { selector: 'button, a[href]' });
  });

  test('renders memo task with reasoning, citations, and tags at 375px without overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={memoTask} />); });
    });
  });

  test('memo section displays summary, reasoning, citations, and tags correctly', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => { r.render(<DetailsTab task={memoTask} />); });

    const content = host.textContent ?? '';
    // Check summary is rendered
    expect(content).toContain('LRU with TTL provides optimal balance');
    // Check reasoning is rendered
    expect(content).toContain('92% cache hit rate under normal load');
    // Check tags are rendered
    expect(content).toContain('optimization');
    expect(content).toContain('performance');
    expect(content).toContain('caching');
    // Check citations are rendered
    expect(content).toContain('task-550e8400e29b41d4a716446655440005');
    expect(content).toContain('direction:d-cache-analysis-1234');
  });

  test('memo text does not silently clip at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={memoTask} />); });
    });
  });

  test('renders the merge-test-gate verdict and output at 375px without overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={mergeTestTask} />); });
    });
  });

  test('merge-test-gate output does not silently clip at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<DetailsTab task={mergeTestTask} />); });
    });
  });

  test('evaluation summary card renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DetailsTab
            task={{
              ...mergeTestTask,
              evaluation_summary: {
                status: 'COMPLETED',
                verdict: 'pass',
                evidence: [
                  { label: 'Tests', value: '23 passed, 0 failed' },
                  { label: 'Gate', value: 'verified on PR head' },
                ],
                highlights: [
                  { label: 'Top', value: 'full suite green' },
                ],
              },
            }}
          />,
        );
      });
    });
  });

  test('shows LLM provider and model when present and hides them otherwise', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => { r.render(<DetailsTab task={llmTask} />); });
    expect(host.textContent ?? '').toContain('LLM Provider');
    expect(host.textContent ?? '').toContain('openai');
    expect(host.textContent ?? '').toContain('LLM Model');
    expect(host.textContent ?? '').toContain('gpt-5-mini');

    await act(async () => { r.render(<DetailsTab task={richTask} />); });
    const content = host.textContent ?? '';
    expect(content).not.toContain('LLM Provider');
    expect(content).not.toContain('LLM Model');
  });
});
