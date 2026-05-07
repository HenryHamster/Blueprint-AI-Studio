import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ResearchBrowser } from './ResearchBrowser';
import type { Task } from '../types/task';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  API_BASE_URL: '',
  authHeaders: vi.fn(() => Promise.resolve({})),
  authGet: vi.fn(() =>
    Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) }),
  ),
  apiFetch: vi.fn(() =>
    Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) }),
  ),
}));

// Stub the directions fetch to bail harmlessly.
globalThis.fetch = vi.fn(() =>
  Promise.resolve({ ok: false, json: () => Promise.resolve({}) }),
) as unknown as typeof fetch;

const sampleTasks: Task[] = [
  {
    id: 'task-research-1',
    title: 'Research: database_connection_timeout_handling resilience patterns',
    description: 'Research task',
    status: 'COMPLETED',
    task_type: 'research',
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-05-06T00:00:00Z',
    research_findings: [
      {
        title: 'Connection pool exhaustion under sustained load',
        description:
          'Under sustained concurrent requests the connection pool hits max_overflow, causing 30s timeouts visible in the p99 latency trace.',
        prompt: 'investigate pool exhaustion',
        scope: 'medium',
        risk: 'high',
        priority: 'high',
        related_files: [
          'backend/app/database.py',
          'backend/app/models.py',
          'backend/testing/test_database_disconnect.py',
        ],
        direction_id: 'dir-database-resilience',
        claims: [
          'pool_size=5, max_overflow=10 — exhausted after 16 concurrent requests',
          'retry logic missing for transient 503s from Cloud SQL proxy',
        ],
        sources: [
          { type: 'file', ref: 'backend/app/database.py:42', excerpt: 'pool_size=5' },
        ],
      },
      {
        title: 'database_connection_timeout_handling: missing exponential backoff',
        description:
          'Retry on transient disconnect uses a fixed 1s sleep; exponential backoff would reduce thundering-herd on reconnect.',
        prompt: 'check retry strategy',
        scope: 'small',
        risk: 'medium',
        priority: 'medium',
        related_files: ['backend/app/database.py'],
        direction_id: 'dir-database-resilience',
      },
    ],
  },
  {
    id: 'task-research-2',
    title: 'Research: cross-cutting pipeline reliability findings',
    description: 'Cross-cutting research',
    status: 'EVALUATING',
    task_type: 'research',
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-05-05T12:00:00Z',
    research_findings: [
      {
        title: 'autonomous_research_diagnosis latency spike on large context',
        description:
          'When the task context exceeds 128k tokens the diagnosis path stalls waiting for a model response — no timeout guard.',
        prompt: 'diagnose latency',
        scope: 'small',
        risk: 'low',
        priority: 'low',
        related_files: ['backend/app/autonomous_research_diagnosis.py'],
      },
    ],
  },
];

describe('ResearchBrowser — viewport regression', () => {
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

  test('off-canvas sidebar renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ResearchBrowser
            tasks={sampleTasks}
            selectedProjectId="project-test"
            onClose={() => {}}
            onNavigateToTask={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });
});
