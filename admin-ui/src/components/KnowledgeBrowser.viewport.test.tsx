import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { fireEvent } from '@testing-library/react';
import { KnowledgeBrowser } from './KnowledgeBrowser';
import { fetchKnowledgeDocuments, fetchKnowledgeDocument } from '../api';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const sampleDocs = [
  {
    id: 'doc-0000-1111-2222-3333',
    title: 'Resilient connection pooling strategies for high-concurrency Postgres workloads',
    summary: 'A survey of pool sizing, overflow handling, and proxy-side retry behaviour under sustained concurrent load with citations.',
    topic: 'database-resilience',
    direction_id: 'd-1a2b3c4d',
    tags: ['postgres', 'pooling', 'reliability', 'cloud-sql'],
    source_citations: [
      'https://www.postgresql.org/docs/current/runtime-config-connection.html',
      { title: 'SQLAlchemy pool docs', url: 'https://docs.sqlalchemy.org/en/20/core/pooling.html', excerpt: 'max_overflow defaults to 10' },
    ],
    source_task_id: 'task-web-research-1',
    authoring_branch: 'feature/knowledge-provenance',
    head_sha: 'abc1234',
    source_kind: 'web_research',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    forward_quality_verdict: { passed: true, reason: 'Well-cited, actionable, verified against current docs.', strategy: 'forward_quality' },
    origin_provenance: {
      origin_state: 'research_finding',
      challenge_state: 'overturned',
      research_task_id: 'task-research-0000-1111-2222-3333-4444',
      finding_index: 3,
      finding_hash: 'sha256:0123456789abcdef0123456789abcdef0123456789abcdef',
      direction_id: 'd-1a2b3c4d',
      finding_challenge: {
        challenged_boundary: 'reproduces_on_head_with_a_very_long_boundary_label_for_overflow',
        first_verdict: 'actionable',
        second_opinion_verdict: 'not_actionable',
        agreement: 'disagrees',
        reason: 'The second reviewer could not reproduce the reported behaviour on HEAD after a careful end-to-end trace.',
        sidecar_task_id: 'task-challenge-aaaa-bbbb-cccc-dddd-eeee',
      },
    },
  },
  {
    id: 'doc-4444-5555-6666-7777',
    title: 'Exponential backoff vs fixed-delay retry under thundering-herd reconnect',
    summary: 'Comparison of reconnect strategies and their effect on recovery latency.',
    topic: 'database-resilience',
    direction_id: null,
    tags: ['retry', 'backoff'],
    source_citations: [],
    source_task_id: null,
    authoring_branch: null,
    head_sha: null,
    source_kind: 'design',
    created_at: '2026-05-30T00:00:00Z',
    updated_at: '2026-05-30T00:00:00Z',
    forward_quality_verdict: { passed: false, reason: 'Insufficient citations.', strategy: 'forward_quality' },
  },
  {
    id: 'doc-8888-9999-aaaa-bbbb',
    title: 'CUDA driver smoke test on L40S GPU',
    summary: 'Raw CUDA driver validation: cuInit, device enumeration, 128MB alloc, DtoH with integrity check.',
    topic: 'gpu-validation',
    direction_id: 'd-compute-1',
    tags: ['compute', 'gpu', 'cuda'],
    source_citations: ['gs://bucket/results/cuda-smoke-result.json'],
    source_task_id: 'task-compute-8888',
    authoring_branch: null,
    head_sha: null,
    source_kind: 'compute_experiment',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    compute_acceptance_grade: 'acceptance',
    criterion_met: true,
    forward_quality_verdict: {
      passed: true,
      strategy: 'metric',
      outcome: 'success',
      terminal_diagnostics: {
        executing_backend: 'local-gpu',
        runner_target_id: 'aaaabbbb-cccc-dddd-eeee-ffff00001111',
        runner_hostname: 'worker-host',
        terminal_reason: 'compute job finished',
        vm_phase: 'runtime',
      },
    },
  },
];

vi.mock('../api', () => ({
  // Transitively-imported modules (TaskCard) read these from '../api'.
  API_BASE_URL: '',
  authGet: vi.fn(() => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) })),
  authHeaders: vi.fn(() => Promise.resolve({})),
  fetchKnowledgeDocuments: vi.fn((projectId: string, params?: Record<string, unknown>) => {
    const q = typeof params?.q === 'string' ? params.q : '';
    if (q.includes('no-match')) {
      return Promise.resolve({ documents: [], total: 0, limit: 50, offset: 0 });
    }
    return Promise.resolve({ documents: sampleDocs, total: sampleDocs.length, limit: 50, offset: 0 });
  }),
  fetchKnowledgeDocument: vi.fn((_projectId: string, docId: string) => {
    const doc = sampleDocs.find(d => d.id === docId) ?? sampleDocs[0];
    return Promise.resolve({ ...doc, body: '# Heading\n\nA paragraph of markdown body text with a [link](https://example.com) and a list:\n\n- one\n- two\n' });
  }),
  resolveArtifactDownloadUrl: vi.fn(() => Promise.resolve('https://signed.example.com/knowledge/doc-0000-1111-2222-3333')),
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

  async function renderList(onLaunchWebResearchTask = vi.fn()) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <KnowledgeBrowser
          selectedProjectId="project-test"
          onClose={() => {}}
          onNavigateToTask={() => {}}
          onLaunchWebResearchTask={onLaunchWebResearchTask}
        />,
      );
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
      const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
        b => b.textContent?.includes('Resilient connection pooling'),
      );
      if (docButton) {
        await act(async () => { docButton.click(); });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
      }
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => { await renderList(); }, { selector: 'button, a[href], [role="button"]' });
  });

  test('empty-state CTA launches web research for the search term at 375px', async () => {
    const onLaunchWebResearchTask = vi.fn();
    await expectNoOverflowAt(375, async () => {
      await renderList(onLaunchWebResearchTask);
      const searchInput = (host as HTMLDivElement).querySelector('input[placeholder*="Search title"]') as HTMLInputElement | null;
      if (searchInput) {
        await act(async () => { fireEvent.change(searchInput, { target: { value: 'no-match topic' } }); });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
      }
      const launchButton = Array.from((host as HTMLDivElement).querySelectorAll('button')).find(b => b.textContent?.includes('Launch web research for'));
      if (launchButton) {
        await act(async () => { fireEvent.click(launchButton); });
      }
    });
    expect(onLaunchWebResearchTask).toHaveBeenCalledWith('no-match topic');
  });

  test('detail refresh launches web research on the selected topic at 375px', async () => {
    const onLaunchWebResearchTask = vi.fn();
    await expectNoOverflowAt(375, async () => {
      await renderList(onLaunchWebResearchTask);
      const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
        b => b.textContent?.includes('Resilient connection pooling'),
      );
      if (docButton) {
        await act(async () => { fireEvent.click(docButton); });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
      }
      const refreshButton = Array.from((host as HTMLDivElement).querySelectorAll('button')).find(b => b.textContent?.includes('refresh research'));
      if (refreshButton) {
        await act(async () => { fireEvent.click(refreshButton); });
      }
    });
    expect(onLaunchWebResearchTask).toHaveBeenCalledWith('database-resilience');
  });

  test('renders an origin badge for each document source kind', async () => {
    await renderList();
    const text = (host as HTMLDivElement).textContent ?? '';
    // Badge labels mirror SOURCE_KIND_STYLES; both sample kinds should be visible.
    expect(text).toContain('Web Research');
    expect(text).toContain('Design');
  });

  test('selecting an origin filter forwards source_kind to fetchKnowledgeDocuments', async () => {
    const mock = vi.mocked(fetchKnowledgeDocuments);
    await renderList();
    mock.mockClear();
    const buttons = (host as HTMLDivElement).querySelectorAll('button');
    const designFilter = Array.from(buttons).find(
      b => b.textContent?.trim() === 'Design' && b.hasAttribute('data-viewport-allow-small-target'),
    );
    expect(designFilter).toBeTruthy();
    if (!designFilter) throw new Error('expected design filter button');
    await act(async () => { designFilter.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    expect(mock).toHaveBeenCalledWith(
      'project-test',
      expect.objectContaining({ source_kind: 'design' }),
    );
  });

  test('selecting a challenge-state filter forwards challenge_state to fetchKnowledgeDocuments', async () => {
    const mock = vi.mocked(fetchKnowledgeDocuments);
    await renderList();
    mock.mockClear();
    const buttons = (host as HTMLDivElement).querySelectorAll('button');
    const overturnedFilter = Array.from(buttons).find(
      b => b.textContent?.trim() === 'Overturned' && b.hasAttribute('data-viewport-allow-small-target'),
    );
    expect(overturnedFilter).toBeTruthy();
    if (!overturnedFilter) throw new Error('expected Overturned filter button');
    await act(async () => { overturnedFilter.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    expect(mock).toHaveBeenCalledWith(
      'project-test',
      expect.objectContaining({ challenge_state: 'overturned' }),
    );
  });

  test('selecting a lifecycle-status filter forwards lifecycle_status to fetchKnowledgeDocuments', async () => {
    const mock = vi.mocked(fetchKnowledgeDocuments);
    await renderList();
    mock.mockClear();
    const buttons = (host as HTMLDivElement).querySelectorAll('button');
    const pendingFilter = Array.from(buttons).find(
      b => b.textContent?.trim() === 'Pending' && b.hasAttribute('data-viewport-allow-small-target'),
    );
    expect(pendingFilter).toBeTruthy();
    if (!pendingFilter) throw new Error('expected Pending filter button');
    await act(async () => { pendingFilter.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    expect(mock).toHaveBeenCalledWith(
      'project-test',
      expect.objectContaining({ lifecycle_status: 'pending' }),
    );
  });

  test('detail view with finding-origin provenance renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      await renderList();
      const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
        b => b.textContent?.includes('Resilient connection pooling'),
      );
      if (docButton) {
        await act(async () => { docButton.click(); });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
      }
    });
    // The finding-origin block and its second-opinion record rendered in the detail view.
    expect((host as HTMLDivElement).textContent).toContain('Finding origin');
    expect((host as HTMLDivElement).textContent).toContain('Second opinion');
  });

  test('compute_experiment detail view renders run provenance with execution location', async () => {
    await renderList();
    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('CUDA driver smoke test'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected compute doc button');
    await act(async () => { (docButton as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Run provenance');
    // criterion_met: true => earned Machine-verified badge (not hardcoded)
    expect(text).toContain('Machine-verified');
    expect(text).toContain('Operator hardware');
    // runner_hostname preferred over truncated runner_target_id UUID
    expect(text).toContain('worker-host');
    expect(text).not.toContain('aaaabbbb');
  });

  test('compute_experiment detail view shows Criterion not met badge when criterion_met is false', async () => {
    vi.mocked(fetchKnowledgeDocuments).mockImplementationOnce(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ((_projectId: string, params?: any) => {
        const q = typeof params?.q === 'string' ? params.q : '';
        if (q.includes('no-match')) {
          return Promise.resolve({ documents: [], total: 0, limit: 50, offset: 0 });
        }
        const docs = sampleDocs.map(d =>
          d.id === 'doc-8888-9999-aaaa-bbbb'
            ? { ...d, criterion_met: false as const, compute_acceptance_grade: 'measured_only' as const }
            : d,
        );
        return Promise.resolve({ documents: docs, total: docs.length, limit: 50, offset: 0 });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      }) as any,
    );
    vi.mocked(fetchKnowledgeDocument).mockImplementationOnce((_projectId, docId) => {
      const base = sampleDocs.find(d => d.id === docId) ?? sampleDocs[0];
      const doc = { ...base, criterion_met: base.id === 'doc-8888-9999-aaaa-bbbb' ? false : (base as { criterion_met?: boolean | null }).criterion_met, compute_acceptance_grade: base.id === 'doc-8888-9999-aaaa-bbbb' ? ('measured_only' as const) : ('acceptance' as const), body: '# Heading\n\nBody.' };
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return Promise.resolve(doc as any);
    });

    await renderList();
    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('CUDA driver smoke test'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected compute doc button');
    await act(async () => { (docButton as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Run provenance');
    expect(text).toContain('Criterion not met');
    expect(text).not.toContain('Machine-verified');
  });

  test('compute_experiment detail view shows Measured only badge when criterion_met is null', async () => {
    vi.mocked(fetchKnowledgeDocuments).mockImplementationOnce(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ((_projectId: string, params?: any) => {
        const q = typeof params?.q === 'string' ? params.q : '';
        if (q.includes('no-match')) {
          return Promise.resolve({ documents: [], total: 0, limit: 50, offset: 0 });
        }
        const docs = sampleDocs.map(d =>
          d.id === 'doc-8888-9999-aaaa-bbbb'
            ? { ...d, criterion_met: null as null, compute_acceptance_grade: 'measured_only' as const }
            : d,
        );
        return Promise.resolve({ documents: docs, total: docs.length, limit: 50, offset: 0 });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      }) as any,
    );
    vi.mocked(fetchKnowledgeDocument).mockImplementationOnce((_projectId, docId) => {
      const base = sampleDocs.find(d => d.id === docId) ?? sampleDocs[0];
      const doc = { ...base, criterion_met: base.id === 'doc-8888-9999-aaaa-bbbb' ? null : (base as { criterion_met?: boolean | null }).criterion_met, compute_acceptance_grade: 'measured_only' as const, body: '# Heading\n\nBody.' };
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return Promise.resolve(doc as any);
    });

    await renderList();
    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('CUDA driver smoke test'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected compute doc button');
    await act(async () => { (docButton as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Run provenance');
    expect(text).toContain('Measured only');
    expect(text).not.toContain('Machine-verified');
  });

  test('detail view renders provenance branch and sha when present', async () => {
    await renderList();
    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('Resilient connection pooling'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected knowledge document button');
    await act(async () => { docButton.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    expect((host as HTMLDivElement).textContent).toContain('feature/knowledge-provenance@abc1234');
  });

  test('compute_experiment detail view with execution provenance renders without overflow at 800px', async () => {
    const computeDocWithProvenance = {
      id: 'doc-compute-provenance-vp',
      title: 'GPU benchmark with execution provenance',
      summary: 'Measured TFLOPS with runner metadata.',
      topic: 'compute-performance',
      direction_id: 'd-compute-1',
      tags: ['gpu', 'benchmark'],
      source_citations: [],
      source_task_id: 'task-ce-vp',
      authoring_branch: null,
      head_sha: null,
      source_kind: 'compute_experiment',
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-01T00:00:00Z',
      knowledge_acceptance_evidence: true,
      compute_acceptance_grade: 'acceptance',
      compute_acceptance_reason: 'llm_artifact_cited',
      criterion_met: true,
      forward_quality_verdict: {
        passed: true,
        reason: 'Artifact-cited analysis passed.',
        strategy: 'forward-quality',
        terminal_diagnostics: {
          executing_backend: 'local-subprocess',
          runner_target_id: 'dd59526e-1234-5678-abcd-ef0123456789',
        },
      },
    };
    const { fetchKnowledgeDocuments: fkd, fetchKnowledgeDocument: fkdoc } = await import('../api');
    vi.mocked(fkd).mockResolvedValueOnce({ documents: [computeDocWithProvenance] as never, total: 1, limit: 50, offset: 0 });
    vi.mocked(fkdoc).mockResolvedValueOnce({ ...computeDocWithProvenance, body: '# GPU Results\n\nThroughput: 312 TFLOPS.' } as never);

    await expectNoOverflowAt(800, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<KnowledgeBrowser selectedProjectId="project-test" onClose={() => {}} onNavigateToTask={() => {}} />);
      });
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
      const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
        b => b.textContent?.includes('GPU benchmark with execution provenance'),
      );
      if (docButton) {
        await act(async () => { (docButton as HTMLElement).click(); });
        await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
      }
    });
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Operator hardware');
    expect(text).toContain('dd59526e');
  });
});
