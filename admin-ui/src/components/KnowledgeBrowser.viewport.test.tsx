import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { KnowledgeBrowser } from './KnowledgeBrowser';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const sampleDocs = [
  {
    id: 'doc-0000-1111-2222-3333',
    title: 'Resilient connection pooling strategies for high-concurrency Postgres workloads',
    summary: 'A survey of pool sizing, overflow handling, and proxy-side retry behaviour under sustained concurrent load with citations.',
    topic: 'database-resilience',
    tags: ['postgres', 'pooling', 'reliability', 'cloud-sql'],
    source_citations: [
      'https://www.postgresql.org/docs/current/runtime-config-connection.html',
      { title: 'SQLAlchemy pool docs', url: 'https://docs.sqlalchemy.org/en/20/core/pooling.html', excerpt: 'max_overflow defaults to 10' },
    ],
    source_task_id: 'task-web-research-1',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    forward_quality_verdict: { passed: true, reason: 'Well-cited, actionable, verified against current docs.', strategy: 'forward_quality' },
  },
  {
    id: 'doc-4444-5555-6666-7777',
    title: 'Exponential backoff vs fixed-delay retry under thundering-herd reconnect',
    summary: 'Comparison of reconnect strategies and their effect on recovery latency.',
    topic: 'database-resilience',
    tags: ['retry', 'backoff'],
    source_citations: [],
    source_task_id: null,
    created_at: '2026-05-30T00:00:00Z',
    updated_at: '2026-05-30T00:00:00Z',
    forward_quality_verdict: { passed: false, reason: 'Insufficient citations.', strategy: 'forward_quality' },
  },
];

vi.mock('../api', () => ({
  // Transitively-imported modules (TaskCard) read these from '../api'.
  API_BASE_URL: '',
  authGet: vi.fn(() => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) })),
  authHeaders: vi.fn(() => Promise.resolve({})),
  fetchKnowledgeDocuments: vi.fn(() =>
    Promise.resolve({ documents: sampleDocs, total: sampleDocs.length, limit: 50, offset: 0 }),
  ),
  fetchKnowledgeDocument: vi.fn(() =>
    Promise.resolve({ ...sampleDocs[0], body: '# Heading\n\nA paragraph of markdown body text with a [link](https://example.com) and a list:\n\n- one\n- two\n' }),
  ),
}));

describe('KnowledgeBrowser — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
  });

  async function renderList() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<KnowledgeBrowser selectedProjectId="project-test" onClose={() => {}} onNavigateToTask={() => {}} />);
    });
    // let the debounced fetch (250ms) resolve and the list render
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
  }

  test('list view renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => { await renderList(); });
  });

  test('document titles and summaries do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => { await renderList(); }, { waitMs: 50 });
  });

  test('detail view renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      await renderList();
      const buttons = (host as HTMLDivElement).querySelectorAll('button');
      const docButton = Array.from(buttons).find(b => b.textContent?.includes('Resilient connection pooling'));
      if (docButton) {
        await act(async () => { docButton.click(); });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
      }
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => { await renderList(); }, { selector: 'button, a[href]' });
  });
});
