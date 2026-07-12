import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { KnowledgeBrowser } from './KnowledgeBrowser';

// A compute_experiment document — proves compute knowledge is recognized as a
// first-class source kind with 'Compute' badge and filter option.
const computeDoc = {
  id: 'doc-compute-experiment-1',
  title: 'GPU benchmark: matrix multiply throughput',
  summary: 'Measured TFLOPS on A100 for various batch sizes.',
  topic: 'compute-performance',
  direction_id: 'd-compute-1',
  tags: ['gpu', 'benchmark'],
  source_citations: ['gs://blueprint-agent-artifacts/jobs/job-1/results.json'],
  source_task_id: 'task-ce-1',
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
    reason: 'Artifact-cited analysis passed acceptance gate.',
    strategy: 'forward-quality',
    probes: {
      grounding: {
        file_citations: 1,
        grounded: 1,
        unresolved: [],
      },
      topic_responsiveness: { passed: true, overlap: 3, total: 3 },
    },
  },
};

// A compute_experiment document graded measured-only: a deterministic fallback
// summary whose analysis mode is not LLM, so the backend classifier withholds the
// acceptance grade. It must stay visible in the list (this is a labeling change,
// not a hide/drop behavior) and carry the 'Measured only' badge.
const measuredOnlyComputeDoc = {
  id: 'doc-compute-experiment-measured',
  title: 'GPU fallback summary: deterministic stats',
  summary: 'Deterministic fallback summary — no LLM analysis.',
  topic: 'compute-performance',
  direction_id: 'd-compute-1',
  tags: ['gpu'],
  source_citations: ['gs://blueprint-agent-artifacts/jobs/job-2/results.json'],
  source_task_id: 'task-ce-2',
  authoring_branch: null,
  head_sha: null,
  source_kind: 'compute_experiment',
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T00:00:00Z',
  knowledge_acceptance_evidence: false,
  compute_acceptance_grade: 'measured_only',
  compute_acceptance_reason: 'analysis_mode_not_llm',
  unavailable_cause: 'analysis-disabled',
  analysis_fallback_reason: 'precondition-not-met',
  criterion_met: false,
  forward_quality_verdict: {
    passed: true,
    reason: 'Deterministic fallback persisted as measured knowledge.',
    strategy: 'forward-quality',
    analysis_mode: 'deterministic',
  },
};

// A web_research document carrying a grounding stamp (cited URLs probed for
// reachability) — proves the Acceptance-probes panel lights up for web_research
// with modality-neutral 'citation(s)' wording, not the design-only 'file
// citation' phrasing.
const webResearchDoc = {
  id: 'doc-web-research-grounded',
  title: 'Survey of reachable web sources',
  summary: 'A web-grounded survey whose cited URLs were probed for reachability.',
  topic: 'web-grounding',
  direction_id: null,
  tags: ['web'],
  source_citations: ['https://example.com/live', 'https://example.com/dead'],
  source_task_id: 'task-wr-1',
  authoring_branch: null,
  head_sha: null,
  source_kind: 'web_research',
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T00:00:00Z',
  forward_quality_verdict: {
    passed: true,
    reason: 'Well-cited and reachable.',
    strategy: 'forward-quality',
    probes: {
      grounding: {
        file_citations: 2,
        grounded: 1,
        unresolved: ['https://example.com/dead'],
      },
      topic_responsiveness: { passed: true, overlap: 2, total: 3 },
    },
  },
};

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authGet: vi.fn(() => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) })),
    authHeaders: vi.fn(() => Promise.resolve({})),
    fetchKnowledgeDocuments: vi.fn(() =>
      Promise.resolve({ documents: [computeDoc, webResearchDoc], total: 2, limit: 50, offset: 0 }),
    ),
    fetchKnowledgeDocument: vi.fn(() =>
      Promise.resolve({ ...webResearchDoc, body: '# Body\n\nMarkdown body.' }),
    ),
    resolveArtifactDownloadUrl: vi.fn(() => Promise.resolve('https://signed.example.com/knowledge/doc-web-research-grounded')),
  });
});

describe('KnowledgeBrowser — routed direction filter', () => {
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

  test('updates the visible direction filter when the routed prop changes while open', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <KnowledgeBrowser
          selectedProjectId="project-test"
          initialDirectionId="direction-alpha"
          onClose={() => {}}
          onNavigateToTask={() => {}}
        />,
      );
    });
    expect(host.textContent).toContain('direction: direction-alpha');

    await act(async () => {
      r.render(
        <KnowledgeBrowser
          selectedProjectId="project-test"
          initialDirectionId="direction-beta"
          onClose={() => {}}
          onNavigateToTask={() => {}}
        />,
      );
    });

    expect(host.textContent).toContain('direction: direction-beta');
    expect(host.textContent).not.toContain('direction: direction-alpha');
  });
});

describe('KnowledgeBrowser — acceptance-probe grounding wording', () => {
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

  async function openDetail() {
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
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('Survey of reachable web sources'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected knowledge document button');
    await act(async () => { docButton.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
  }

  test('renders web_research grounding with modality-neutral citation wording', async () => {
    await openDetail();
    const text = (host as HTMLDivElement).textContent ?? '';
    // Modality-neutral copy — NOT the old design-only 'file citation' phrasing.
    expect(text).toContain('Grounded 1 of 2 citations');
    expect(text).not.toContain('file citation');
    // The dead URL surfaces under the unresolved list.
    expect(text).toContain('https://example.com/dead');
  });

  test('singular citation count drops the plural s', async () => {
    // Rebind the mock so a single-citation doc renders.
    const { fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocument).mockResolvedValueOnce({
      ...webResearchDoc,
      body: '# Body',
      forward_quality_verdict: {
        ...webResearchDoc.forward_quality_verdict,
        probes: {
          grounding: { file_citations: 1, grounded: 1, unresolved: [] },
          topic_responsiveness: { passed: true, overlap: 2, total: 3 },
        },
      },
    } as never);
    await openDetail();
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Grounded 1 of 1 citation');
    expect(text).not.toContain('1 of 1 citations');
  });

  test('resolves gs:// citations through the signed download API', async () => {
    const { fetchKnowledgeDocument, resolveArtifactDownloadUrl } = await import('../api');
    vi.mocked(fetchKnowledgeDocument).mockResolvedValueOnce({
      ...webResearchDoc,
      body: '# Body',
      source_citations: ['gs://blueprint-agent-artifacts/tasks/task-wr-1/citations/report.pdf'],
    } as never);
    const openSpy = vi.spyOn(window, 'open').mockReturnValue({ opener: null, location: { replace: vi.fn() }, close: vi.fn() } as never);
    await openDetail();
    const citationButton = Array.from((host as HTMLDivElement).querySelectorAll('button')).find(
      (button) => button.textContent?.includes('gs://blueprint-agent-artifacts/tasks/task-wr-1/citations/report.pdf'),
    );
    expect(citationButton).toBeTruthy();
    if (!citationButton) throw new Error('expected gs:// citation button');
    await act(async () => { citationButton.click(); });
    expect(vi.mocked(resolveArtifactDownloadUrl)).toHaveBeenCalledWith(
      'project-test',
      'gs://blueprint-agent-artifacts/tasks/task-wr-1/citations/report.pdf',
    );
    expect(openSpy).toHaveBeenCalled();
  });

  test('renders null citations as plain text instead of crashing', async () => {
    const { fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocument).mockResolvedValueOnce({
      ...webResearchDoc,
      body: '# Body',
      source_citations: [null, 'https://example.com/live'],
    } as never);
    await openDetail();
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('null');
    expect(text).toContain('https://example.com/live');
  });
});

describe('KnowledgeBrowser — criterion verdict badge', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
    vi.resetAllMocks();
  });

  test('renders criterion MET badge for compute_experiment documents', async () => {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');

    const metDoc = { ...computeDoc, criterion_met: true };
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({
      documents: [metDoc],
      total: 1,
      limit: 50,
      offset: 0
    });
    vi.mocked(fetchKnowledgeDocument).mockResolvedValue({
      ...metDoc,
      body: '# Results\n\nCriterion was met.'
    } as never);

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <KnowledgeBrowser
          selectedProjectId="project-test"
          onClose={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });

    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('GPU benchmark'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected document button');
    await act(async () => { docButton.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Criterion MET');
  });

  test('renders criterion NOT MET badge for compute_experiment documents', async () => {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');

    const notMetDoc = { ...measuredOnlyComputeDoc, criterion_met: false };
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({
      documents: [notMetDoc],
      total: 1,
      limit: 50,
      offset: 0
    });
    vi.mocked(fetchKnowledgeDocument).mockResolvedValue({
      ...notMetDoc,
      body: '# Results\n\nCriterion was not met.'
    } as never);

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <KnowledgeBrowser
          selectedProjectId="project-test"
          onClose={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });

    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('deterministic'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected document button');
    await act(async () => { docButton.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Criterion NOT MET');
  });

  test('renders criterion UNMEASURED badge when criterion_met is null', async () => {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');

    const unmeasuredDoc = { ...computeDoc, id: 'doc-compute-unmeasured', criterion_met: null };
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({
      documents: [unmeasuredDoc],
      total: 1,
      limit: 50,
      offset: 0
    });
    vi.mocked(fetchKnowledgeDocument).mockResolvedValue({
      ...unmeasuredDoc,
      body: '# Results'
    } as never);

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;

    await act(async () => {
      r.render(
        <KnowledgeBrowser
          selectedProjectId="project-test"
          onClose={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });

    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('GPU benchmark'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected document button');
    await act(async () => { docButton.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Criterion UNMEASURED');
  });
});

describe('KnowledgeBrowser — pagination', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
    vi.resetAllMocks();
  });

  const page1Doc = {
    id: 'doc-page1',
    title: 'First page document',
    summary: 'Doc from first page.',
    topic: null,
    direction_id: null,
    tags: [],
    source_citations: [],
    source_task_id: null,
    authoring_branch: null,
    head_sha: null,
    source_kind: 'web_research',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    forward_quality_verdict: null,
  };

  const page2Doc = {
    id: 'doc-page2',
    title: 'Second page document',
    summary: 'Doc from second page.',
    topic: null,
    direction_id: null,
    tags: [],
    source_citations: [],
    source_task_id: null,
    authoring_branch: null,
    head_sha: null,
    source_kind: 'web_research',
    created_at: '2026-05-31T00:00:00Z',
    updated_at: '2026-05-31T00:00:00Z',
    forward_quality_verdict: null,
  };

  async function renderBrowser() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <KnowledgeBrowser
          selectedProjectId="project-test"
          onClose={() => {}}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    return host as HTMLDivElement;
  }

  test('shows load-more button when docs.length < total', async () => {
    const { fetchKnowledgeDocuments } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({ documents: [page1Doc], total: 2, limit: 50, offset: 0 });

    const container = await renderBrowser();
    const loadMore = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.includes('Load more'));
    expect(loadMore).toBeTruthy();
  });

  test('load-more appends second page to list', async () => {
    const { fetchKnowledgeDocuments } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments)
      .mockResolvedValueOnce({ documents: [page1Doc], total: 2, limit: 50, offset: 0 })
      .mockResolvedValueOnce({ documents: [page2Doc], total: 2, limit: 50, offset: 50 });

    const container = await renderBrowser();

    const loadMore = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.includes('Load more'));
    expect(loadMore).toBeTruthy();
    if (!loadMore) throw new Error('expected load more button');

    await act(async () => { loadMore.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });

    const text = container.textContent ?? '';
    expect(text).toContain('First page document');
    expect(text).toContain('Second page document');
    // No load-more button when all docs shown
    const loadMoreAfter = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.trim() === 'Load more');
    expect(loadMoreAfter).toBeFalsy();
  });

  test('header shows "showing N of TOTAL" when paginated', async () => {
    const { fetchKnowledgeDocuments } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({ documents: [page1Doc], total: 5, limit: 50, offset: 0 });

    const container = await renderBrowser();
    const text = container.textContent ?? '';
    expect(text).toContain('showing 1 of 5');
  });

  test('changing search filter resets to first page', async () => {
    const { fetchKnowledgeDocuments } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments)
      .mockResolvedValueOnce({ documents: [page1Doc], total: 2, limit: 50, offset: 0 })
      .mockResolvedValueOnce({ documents: [page2Doc], total: 2, limit: 50, offset: 50 })
      .mockResolvedValue({ documents: [page1Doc], total: 1, limit: 50, offset: 0 });

    const container = await renderBrowser();

    // Load a second page
    const loadMore = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.includes('Load more'));
    if (!loadMore) throw new Error('expected load more button');
    await act(async () => { loadMore.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });

    // Change the search input — should reset to offset 0
    const searchInput = container.querySelector('input[type="text"]') as HTMLInputElement;
    expect(searchInput).toBeTruthy();
    await act(async () => {
      // Simulate React's onChange via native input event (React 18 jsdom pattern)
      const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
      nativeInputValueSetter?.call(searchInput, 'new query');
      searchInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });

    // The last call to fetchKnowledgeDocuments should have offset=0
    const calls = vi.mocked(fetchKnowledgeDocuments).mock.calls;
    const lastCall = calls[calls.length - 1];
    expect(lastCall[1]?.offset).toBe(0);
  });

  test('changing source-kind filter resets to first page', async () => {
    const { fetchKnowledgeDocuments } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments)
      .mockResolvedValueOnce({ documents: [page1Doc], total: 2, limit: 50, offset: 0 })
      .mockResolvedValueOnce({ documents: [page2Doc], total: 2, limit: 50, offset: 50 })
      .mockResolvedValue({ documents: [page1Doc], total: 1, limit: 50, offset: 0 });

    const container = await renderBrowser();

    // Load a second page
    const loadMore = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.includes('Load more'));
    if (!loadMore) throw new Error('expected load more button');
    await act(async () => { loadMore.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });

    // Click the 'Design' origin filter button — changes sourceKindFilter, should reset offset
    const designButton = Array.from(container.querySelectorAll('button')).find(
      b => b.textContent?.trim() === 'Design',
    );
    expect(designButton).toBeTruthy();
    if (!designButton) throw new Error('expected Design filter button');
    await act(async () => { designButton.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });

    const calls = vi.mocked(fetchKnowledgeDocuments).mock.calls;
    const lastCall = calls[calls.length - 1];
    expect(lastCall[1]?.offset).toBe(0);
    expect(lastCall[1]?.source_kind).toBe('design');
  });

  test('no load-more button when all docs fit on one page', async () => {
    const { fetchKnowledgeDocuments } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({ documents: [page1Doc], total: 1, limit: 50, offset: 0 });

    const container = await renderBrowser();
    const loadMore = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.trim() === 'Load more');
    expect(loadMore).toBeFalsy();
  });
});

describe('KnowledgeBrowser — compute_experiment source kind', () => {
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

  async function renderWithComputeDoc() {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValueOnce({ documents: [computeDoc], total: 1, limit: 50, offset: 0 });
    vi.mocked(fetchKnowledgeDocument).mockResolvedValueOnce({ ...computeDoc, body: '# Compute analysis\n\nResults here.' } as never);

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
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
  }

  test('renders compute_experiment as a first-class source kind and filter option', async () => {
    await renderWithComputeDoc();

    // The document card should have a badge that says 'Compute'.
    const docCard = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('GPU benchmark'),
    );
    expect(docCard).toBeTruthy();
    // Badge within the document card should read 'Compute', not 'Unknown'.
    expect(docCard?.textContent).toContain('Compute');
    expect(docCard?.textContent).not.toContain('Unknown');

    // Filter bar exposes a 'Compute' button.
    const computeButton = Array.from((host as HTMLDivElement).querySelectorAll('button')).find(
      b => b.textContent?.trim() === 'Compute',
    );
    expect(computeButton).toBeTruthy();
  });

  test('compute document with grounding probe surfaces acceptance detail in detail view', async () => {
    await renderWithComputeDoc();
    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('GPU benchmark'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected compute document button');
    await act(async () => { docButton.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Grounded 1 of 1 citation');
    expect(text).toContain('Acceptance probes');
  });
});

describe('KnowledgeBrowser — compute acceptance grade', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
    vi.resetAllMocks();
  });

  // Locate a rendered badge by its exact trimmed label. Trimming on the exact
  // string keeps 'Acceptance' distinct from the 'Acceptance probes' panel header.
  function findBadge(scope: ParentNode, label: string): Element | undefined {
    return Array.from(scope.querySelectorAll('span')).find(
      s => s.textContent?.trim() === label,
    );
  }

  async function renderList(documents: unknown[]) {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({
      documents: documents as never,
      total: documents.length,
      limit: 50,
      offset: 0,
    });
    vi.mocked(fetchKnowledgeDocument).mockImplementation((_pid, id) =>
      Promise.resolve({
        ...(documents.find(d => (d as { id: string }).id === id) as object),
        body: '# Compute analysis\n\nResults here.',
      } as never),
    );

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
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
  }

  test('renders_compute_acceptance_grade_and_measured_only_badges', async () => {
    await renderList([computeDoc, measuredOnlyComputeDoc]);

    // Both documents stay visible in the list — measured-only is labeled, not hidden.
    const acceptanceCard = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('GPU benchmark'),
    );
    const measuredCard = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('GPU fallback summary'),
    );
    expect(acceptanceCard).toBeTruthy();
    expect(measuredCard).toBeTruthy();

    // Each card carries its own acceptance-grade badge alongside the 'Compute' source badge.
    expect(acceptanceCard && findBadge(acceptanceCard, 'Acceptance')).toBeTruthy();
    expect(acceptanceCard?.textContent).toContain('Compute');
    expect(acceptanceCard?.textContent).not.toContain('LLM credentials missing');
    expect(measuredCard && findBadge(measuredCard, 'Measured only')).toBeTruthy();
    expect(measuredCard?.textContent).toContain('Compute');
    expect(measuredCard?.textContent).toContain('Analysis disabled');

    // Open the acceptance-grade document — its badge also renders in the detail meta row.
    if (!acceptanceCard) throw new Error('expected acceptance compute card');
    await act(async () => { (acceptanceCard as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    expect(findBadge(host as HTMLDivElement, 'Acceptance')).toBeTruthy();

    // Return to the list so the measured-only row can be clicked from a live DOM node.
    await renderList([computeDoc, measuredOnlyComputeDoc]);

    // Open the measured-only document and verify the concrete fallback cause shows up.
    const measuredCardAgain = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('GPU fallback summary'),
    );
    if (!measuredCardAgain) throw new Error('expected measured compute card');
    await act(async () => { (measuredCardAgain as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const detailText = (host as HTMLDivElement).textContent ?? '';
    expect(detailText).toContain('Measured only');
    expect(detailText).toContain('Analysis disabled');
  });

  test('legacy measured-only compute docs render without cause metadata', async () => {
    const legacyMeasuredOnlyComputeDoc = {
      ...measuredOnlyComputeDoc,
      id: 'doc-compute-experiment-legacy-measured',
      title: 'Legacy fallback summary',
      unavailable_cause: null,
      analysis_fallback_reason: null,
      forward_quality_verdict: {
        ...measuredOnlyComputeDoc.forward_quality_verdict,
        analysis_mode: 'deterministic',
      },
    };
    await renderList([legacyMeasuredOnlyComputeDoc]);

    const docCard = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('Legacy fallback summary'),
    );
    expect(docCard).toBeTruthy();
    expect(docCard?.textContent).toContain('Measured only');
    expect(docCard?.textContent).not.toContain('Analysis disabled');
    expect(docCard?.textContent).not.toContain('Missing credentials');
  });

  test('non-acceptance grade is absent for documents without the compute grade', async () => {
    // A compute doc serialized before the grade field existed (grade undefined)
    // renders no acceptance badge but stays a first-class 'Compute' document.
    const ungraded = { ...computeDoc, id: 'doc-ungraded', title: 'GPU legacy doc', compute_acceptance_grade: undefined, compute_acceptance_reason: undefined };
    await renderList([ungraded]);
    const card = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('GPU legacy doc'),
    );
    expect(card).toBeTruthy();
    expect(card?.textContent).toContain('Compute');
    expect(card && findBadge(card, 'Acceptance')).toBeFalsy();
    expect(card && findBadge(card, 'Measured only')).toBeFalsy();
  });
});

describe('KnowledgeBrowser — finding-challenge origin provenance', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
    vi.resetAllMocks();
  });

  function findBadge(scope: ParentNode, label: string): Element | undefined {
    return Array.from(scope.querySelectorAll('span')).find(
      s => s.textContent?.trim() === label,
    );
  }

  // Knowledge documents spawned from research findings, each carrying the backend
  // origin-provenance object (serialize_origin_provenance) with a distinct resolved
  // second-opinion challenge state.
  const findingDoc = (overrides: Record<string, unknown>) => ({
    id: 'doc-finding',
    title: 'Finding-derived design',
    summary: 'A design doc spawned from a challenged research finding.',
    topic: 'resilience',
    direction_id: 'd-finding-1',
    tags: ['origin'],
    source_citations: [],
    source_task_id: 'task-design-1',
    authoring_branch: null,
    head_sha: null,
    source_kind: 'design',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    forward_quality_verdict: { passed: true, reason: 'ok', strategy: 'forward-quality' },
    ...overrides,
  });

  const confirmedDoc = findingDoc({
    id: 'doc-confirmed', title: 'Confirmed finding design',
    origin_provenance: {
      origin_state: 'research_finding',
      challenge_state: 'challenger_confirmed',
      research_task_id: 'task-research-abcdef12',
      parent_research_task_id: 'task-research-parent-9',
      finding_index: 2,
      finding_hash: 'sha256:deadbeefcafe',
      direction_id: 'd-finding-1',
      finding_challenge: {
        challenged_boundary: 'reproduces_on_head',
        first_verdict: 'actionable',
        second_opinion_verdict: 'actionable',
        agreement: 'agrees',
        reason: 'Second reviewer reproduced the finding on HEAD.',
        sidecar_task_id: 'task-challenge-77',
      },
    },
  });
  const overturnedDoc = findingDoc({
    id: 'doc-overturned', title: 'Overturned finding design',
    origin_provenance: {
      origin_state: 'research_finding', challenge_state: 'overturned',
      research_task_id: 'task-research-2',
      finding_challenge: { agreement: 'disagrees', first_verdict: 'actionable', second_opinion_verdict: 'not_actionable', reason: 'Could not reproduce.' },
    },
  });
  const indeterminateDoc = findingDoc({
    id: 'doc-indeterminate', title: 'Indeterminate finding design',
    origin_provenance: {
      origin_state: 'research_finding', challenge_state: 'indeterminate_fail_closed',
      research_task_id: 'task-research-3',
      finding_challenge: { agreement: 'agrees', second_opinion_verdict: 'indeterminate' },
    },
  });
  const unchallengedDoc = findingDoc({
    id: 'doc-unchallenged', title: 'Unchallenged finding design',
    origin_provenance: { origin_state: 'research_finding', challenge_state: 'unchallenged', research_task_id: 'task-research-4' },
  });
  // A document with no origin provenance at all — older backend / legacy doc.
  const legacyDoc = findingDoc({ id: 'doc-legacy', title: 'Legacy design without provenance' });
  // A non-finding-derived document — provenance present but the challenge axis does not apply.
  const nonFindingDoc = findingDoc({
    id: 'doc-non-finding', title: 'Non-finding derived design',
    origin_provenance: { origin_state: 'not_research_finding_derived', challenge_state: 'not_applicable' },
  });

  async function renderList(documents: unknown[]) {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({
      documents: documents as never,
      total: documents.length,
      limit: 50,
      offset: 0,
    });
    vi.mocked(fetchKnowledgeDocument).mockImplementation((_pid, id) =>
      Promise.resolve({
        ...(documents.find(d => (d as { id: string }).id === id) as object),
        body: '# Body\n\nMarkdown body.',
      } as never),
    );

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <KnowledgeBrowser selectedProjectId="project-test" onClose={() => {}} onNavigateToTask={() => {}} />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
  }

  function cardFor(title: string): Element | undefined {
    return Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes(title),
    );
  }

  test('renders_finding_challenge_origin_provenance', async () => {
    await renderList([confirmedDoc, overturnedDoc, indeterminateDoc, unchallengedDoc, legacyDoc, nonFindingDoc]);

    // Each finding-derived card carries the challenge-state badge matching its
    // resolved second-opinion outcome.
    const confirmedCard = cardFor('Confirmed finding design');
    const overturnedCard = cardFor('Overturned finding design');
    const indeterminateCard = cardFor('Indeterminate finding design');
    const unchallengedCard = cardFor('Unchallenged finding design');
    const legacyCard = cardFor('Legacy design without provenance');
    const nonFindingCard = cardFor('Non-finding derived design');
    expect(confirmedCard && findBadge(confirmedCard, 'Confirmed')).toBeTruthy();
    expect(overturnedCard && findBadge(overturnedCard, 'Overturned')).toBeTruthy();
    expect(indeterminateCard && findBadge(indeterminateCard, 'Indeterminate')).toBeTruthy();
    expect(unchallengedCard && findBadge(unchallengedCard, 'Unchallenged')).toBeTruthy();

    // Legacy (no provenance) and not-research-finding-derived docs carry no
    // challenge badge in the list — they stay visible and clean.
    for (const label of ['Confirmed', 'Overturned', 'Indeterminate', 'Unchallenged']) {
      expect(legacyCard && findBadge(legacyCard, label)).toBeFalsy();
      expect(nonFindingCard && findBadge(nonFindingCard, label)).toBeFalsy();
    }

    // Open the confirmed document — the detail block exposes the source research
    // task / finding identity and the second-opinion decision record.
    if (!confirmedCard) throw new Error('expected confirmed finding card');
    await act(async () => { (confirmedCard as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    const detailText = (host as HTMLDivElement).textContent ?? '';
    expect(detailText).toContain('Finding origin');
    expect(detailText).toContain('Research finding');
    expect(detailText).toContain('task-res'); // research task id (truncated link prefix)
    expect(detailText).toContain('Second opinion');
    expect(detailText).toContain('reproduces_on_head'); // challenged boundary
    expect(detailText).toContain('agrees'); // agreement
    expect(detailText).toContain('Second reviewer reproduced the finding on HEAD.'); // reason
  });

  test('legacy document detail view omits the finding-origin block when provenance is absent', async () => {
    await renderList([legacyDoc]);
    const card = cardFor('Legacy design without provenance');
    if (!card) throw new Error('expected legacy card');
    await act(async () => { (card as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    // No origin_provenance → no finding-origin block, but the document still renders.
    expect((host as HTMLDivElement).textContent).not.toContain('Finding origin');
    expect((host as HTMLDivElement).textContent).toContain('Legacy design without provenance');
  });

  test('challenge-state filter forwards challenge_state to fetchKnowledgeDocuments', async () => {
    await renderList([confirmedDoc]);
    const { fetchKnowledgeDocuments } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockClear();
    const overturnedFilter = Array.from((host as HTMLDivElement).querySelectorAll('button')).find(
      b => b.textContent?.trim() === 'Overturned',
    );
    expect(overturnedFilter).toBeTruthy();
    if (!overturnedFilter) throw new Error('expected Overturned filter button');
    await act(async () => { overturnedFilter.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const calls = vi.mocked(fetchKnowledgeDocuments).mock.calls;
    const lastCall = calls[calls.length - 1];
    expect(lastCall[1]?.offset).toBe(0);
    expect(lastCall[1]?.challenge_state).toBe('overturned');
  });
});

describe('KnowledgeBrowser — lifecycle status badge', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
    vi.resetAllMocks();
  });

  function findBadge(scope: ParentNode, label: string): Element | undefined {
    return Array.from(scope.querySelectorAll('span')).find(
      s => s.textContent?.trim() === label,
    );
  }

  async function renderList(documents: unknown[]) {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({
      documents: documents as never,
      total: documents.length,
      limit: 50,
      offset: 0,
    });
    vi.mocked(fetchKnowledgeDocument).mockImplementation((_pid, id) =>
      Promise.resolve({
        ...(documents.find(d => (d as { id: string }).id === id) as object),
        body: '# Body\n\nMarkdown body.',
      } as never),
    );

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
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
  }

  function cardFor(title: string): Element | undefined {
    return Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes(title),
    );
  }

  test('renders consumed badge when lifecycle_status is consumed', async () => {
    const consumedDoc = { ...computeDoc, id: 'doc-lifecycle-consumed', title: 'Consumed lifecycle doc', lifecycle_status: 'consumed' };
    await renderList([consumedDoc]);
    const card = cardFor('Consumed lifecycle doc');
    expect(card && findBadge(card, 'Consumed')).toBeTruthy();
  });

  test('renders dismissed badge when lifecycle_status is dismissed', async () => {
    const dismissedDoc = { ...computeDoc, id: 'doc-lifecycle-dismissed', title: 'Dismissed lifecycle doc', lifecycle_status: 'dismissed' };
    await renderList([dismissedDoc]);
    const card = cardFor('Dismissed lifecycle doc');
    expect(card && findBadge(card, 'Dismissed')).toBeTruthy();
  });

  test('no badge when lifecycle_status is pending', async () => {
    const pendingDoc = { ...computeDoc, id: 'doc-lifecycle-pending', title: 'Pending lifecycle doc', lifecycle_status: 'pending' };
    await renderList([pendingDoc]);
    const card = cardFor('Pending lifecycle doc');
    expect(card && findBadge(card, 'Consumed')).toBeFalsy();
    expect(card && findBadge(card, 'Dismissed')).toBeFalsy();
  });

  test('no badge when lifecycle_status is null', async () => {
    const nullDoc: Record<string, unknown> = { ...computeDoc, id: 'doc-lifecycle-null', title: 'Null lifecycle doc' };
    nullDoc.lifecycle_status = null;
    await renderList([nullDoc]);
    const card = cardFor('Null lifecycle doc');
    expect(card && findBadge(card, 'Consumed')).toBeFalsy();
    expect(card && findBadge(card, 'Dismissed')).toBeFalsy();
  });

  test('no badge when lifecycle_status is undefined (absent field)', async () => {
    const undefinedDoc = { ...computeDoc, id: 'doc-lifecycle-undef', title: 'Undefined lifecycle doc' };
    await renderList([undefinedDoc]);
    const card = cardFor('Undefined lifecycle doc');
    expect(card && findBadge(card, 'Consumed')).toBeFalsy();
    expect(card && findBadge(card, 'Dismissed')).toBeFalsy();
  });

  test('consumed badge appears in detail view', async () => {
    const consumedDoc = { ...computeDoc, id: 'doc-lifecycle-consumed-detail', title: 'Consumed lifecycle detail', lifecycle_status: 'consumed' };
    await renderList([consumedDoc]);
    const card = cardFor('Consumed lifecycle detail');
    expect(card).toBeTruthy();
    if (!card) throw new Error('expected document card');
    await act(async () => { (card as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    expect((host as HTMLDivElement).textContent).toContain('Consumed');
  });

  test('dismissed badge appears in detail view', async () => {
    const dismissedDoc = { ...computeDoc, id: 'doc-lifecycle-dismissed-detail', title: 'Dismissed lifecycle detail', lifecycle_status: 'dismissed' };
    await renderList([dismissedDoc]);
    const card = cardFor('Dismissed lifecycle detail');
    expect(card).toBeTruthy();
    if (!card) throw new Error('expected document card');
    await act(async () => { (card as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    expect((host as HTMLDivElement).textContent).toContain('Dismissed');
  });

  test('lifecycle filter forwards lifecycle_status to fetchKnowledgeDocuments', async () => {
    await renderList([computeDoc]);
    const { fetchKnowledgeDocuments } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockClear();
    const pendingFilter = Array.from((host as HTMLDivElement).querySelectorAll('button')).find(
      b => b.textContent?.trim() === 'Pending',
    );
    expect(pendingFilter).toBeTruthy();
    if (!pendingFilter) throw new Error('expected Pending filter button');
    await act(async () => { pendingFilter.click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const calls = vi.mocked(fetchKnowledgeDocuments).mock.calls;
    const lastCall = calls[calls.length - 1];
    expect(lastCall[1]?.offset).toBe(0);
    expect(lastCall[1]?.lifecycle_status).toBe('pending');
  });
});

describe('KnowledgeBrowser — document body load failure', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
    vi.resetAllMocks();
  });

  async function renderBrowser(props: { initialDocumentId?: string } = {}) {
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
          {...props}
        />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    return host as HTMLDivElement;
  }

  test('clicking a document whose body fetch fails shows doc-error and suppresses the loading placeholder', async () => {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({
      documents: [webResearchDoc], total: 1, limit: 50, offset: 0,
    });
    vi.mocked(fetchKnowledgeDocument).mockRejectedValue(new Error('body fetch failed'));

    const container = await renderBrowser();
    const docButton = Array.from(container.querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes('Survey of reachable web sources'),
    );
    expect(docButton).toBeTruthy();
    if (!docButton) throw new Error('expected knowledge document button');
    await act(async () => { (docButton as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });

    const errorEl = container.querySelector('[data-testid="doc-error"]');
    expect(errorEl).toBeTruthy();
    expect(errorEl?.textContent).toContain('Failed to load document body.');
    // The optimistic detail view stays open (title visible) but the body
    // placeholder must be suppressed in favor of the error message.
    expect(container.textContent).toContain('Survey of reachable web sources');
    expect(container.textContent).not.toContain('Loading document…');
  });

  test('initialDocumentId body fetch failure surfaces doc-error in the list view', async () => {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({
      documents: [webResearchDoc], total: 1, limit: 50, offset: 0,
    });
    vi.mocked(fetchKnowledgeDocument).mockRejectedValue(new Error('body fetch failed'));

    const container = await renderBrowser({ initialDocumentId: 'doc-web-research-grounded' });

    // The detail view never opens (no document to select), so the error must
    // be visible from the list view instead of failing silently.
    const errorEl = container.querySelector('[data-testid="doc-error"]');
    expect(errorEl).toBeTruthy();
    expect(errorEl?.textContent).toContain('Failed to load document body.');
    expect(container.textContent).not.toContain('Loading document…');
  });
});

describe('KnowledgeBrowser — live proof release evidence', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
    vi.resetAllMocks();
  });

  // A compute_experiment document whose launching job ran as a live-release proof:
  // the backend surfaces the promoted serving evidence as a top-level field.
  const liveProofDoc = {
    ...computeDoc,
    id: 'doc-live-proof',
    title: 'GPU live-proof benchmark',
    release_evidence: {
      promoted_sha: 'abcdef1234567890deadbeef',
      promoted_at: '2026-06-05T00:00:00Z',
      aggregate_serving_status: 'serving_confirmed',
      aggregate_reason_code: null,
      release_version_id: 'rv-8',
      release_version_number: 8,
      required_artifacts: [
        {
          artifact_kind: 'backend_revision',
          required: true,
          serving_status: 'serving_confirmed',
          served_identifier: 'abcdef1234567890',
          served_at: '2026-06-05T00:00:00Z',
          reason_code: null,
        },
        {
          artifact_kind: 'agent_image',
          required: true,
          serving_status: 'deploy_failed',
          served_identifier: null,
          served_at: null,
          reason_code: 'agent image not reachable',
        },
      ],
    },
  };

  async function renderAndOpen(doc: unknown) {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({ documents: [doc] as never, total: 1, limit: 50, offset: 0 });
    vi.mocked(fetchKnowledgeDocument).mockResolvedValue({ ...(doc as object), body: '# Body' } as never);

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <KnowledgeBrowser selectedProjectId="project-test" onClose={() => {}} onNavigateToTask={() => {}} />,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const card = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes((doc as { title: string }).title),
    );
    if (!card) throw new Error('expected document card');
    await act(async () => { (card as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
  }

  test('live-proof compute doc shows the Live Proof Context block with version, short SHA, and serving status', async () => {
    await renderAndOpen(liveProofDoc);
    const panel = (host as HTMLDivElement).querySelector('[data-testid="release-evidence-panel"]');
    expect(panel).toBeTruthy();
    const text = panel?.textContent ?? '';
    expect(text).toContain('Live Proof Context');
    expect(text).toContain('v8');
    // Short SHA = first 8 chars of the promoted SHA, not the full hash.
    expect(text).toContain('abcdef12');
    expect(text).not.toContain('abcdef1234567890deadbeef');
    expect(text).toContain('serving_confirmed');
    expect(text).toContain('Required artifacts');
    expect(text).toContain('backend_revision');
    expect(text).toContain('agent_image');
    expect(text).toContain('deploy_failed');
    expect(text).toContain('agent image not reachable');
  });

  test('compute doc without release_evidence shows no Live Proof Context block', async () => {
    const noEvidenceDoc = { ...computeDoc, id: 'doc-no-evidence', title: 'GPU no-proof benchmark', release_evidence: null };
    await renderAndOpen(noEvidenceDoc);
    expect((host as HTMLDivElement).querySelector('[data-testid="release-evidence-panel"]')).toBeFalsy();
    // The document itself still renders.
    expect((host as HTMLDivElement).textContent).toContain('GPU no-proof benchmark');
  });
});

describe('KnowledgeBrowser — compute run provenance detail', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) { host.remove(); host = null; }
    vi.resetAllMocks();
  });

  async function renderAndOpenDoc(doc: typeof computeDoc & { forward_quality_verdict?: unknown }) {
    const { fetchKnowledgeDocuments, fetchKnowledgeDocument } = await import('../api');
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({ documents: [doc] as never, total: 1, limit: 50, offset: 0 });
    vi.mocked(fetchKnowledgeDocument).mockResolvedValue({ ...doc, body: '# Body' } as never);

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<KnowledgeBrowser selectedProjectId="project-test" onClose={() => {}} />);
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 350)); });
    const docButton = Array.from((host as HTMLDivElement).querySelectorAll('[role="button"]')).find(
      b => b.textContent?.includes(doc.title),
    );
    if (!docButton) throw new Error('expected document button');
    await act(async () => { (docButton as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
  }

  test('renders run provenance with operator-hardware label and truncated runner id for local-subprocess', async () => {
    const docWithProvenance = {
      ...computeDoc,
      id: 'doc-provenance-1',
      title: 'GPU benchmark with provenance',
      evidence_tier: 'covered' as const,
      forward_quality_verdict: {
        ...computeDoc.forward_quality_verdict,
        terminal_diagnostics: {
          executing_backend: 'local-subprocess',
          runner_target_id: 'abc12345xyz99999',
        },
      },
    };
    await renderAndOpenDoc(docWithProvenance);
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Run provenance');
    expect(text).toContain('Machine verified');
    expect(text).toContain('Operator hardware');
    expect(text).toContain('abc12345');
    expect(text).not.toContain('abc12345xyz');
  });

  test('renders GPU label for local-gpu backend', async () => {
    const docWithGpu = {
      ...computeDoc,
      id: 'doc-provenance-gpu',
      title: 'GPU experiment with gpu backend',
      evidence_tier: 'covered' as const,
      forward_quality_verdict: {
        ...computeDoc.forward_quality_verdict,
        terminal_diagnostics: {
          executing_backend: 'local-gpu',
          runner_target_id: 'gpu-runner-0001',
        },
      },
    };
    await renderAndOpenDoc(docWithGpu);
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Machine verified');
    expect(text).toContain('Operator hardware · GPU');
    expect(text).toContain('gpu-runn');
  });

  test('renders GCP label for gcp backend', async () => {
    const docWithGcp = {
      ...computeDoc,
      id: 'doc-provenance-gcp',
      title: 'Cloud experiment with gcp backend',
      evidence_tier: 'attested' as const,
      forward_quality_verdict: {
        ...computeDoc.forward_quality_verdict,
        terminal_diagnostics: {
          executing_backend: 'gcp',
          runner_target_id: null,
        },
      },
    };
    await renderAndOpenDoc(docWithGcp);
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Run provenance');
    expect(text).toContain('GCP cloud');
    expect(text).toContain('Attested');
    expect(text).not.toContain('Machine-verified');
    expect(text).not.toContain('Machine verified');
  });

  test('does not render duplicate Machine-verified badge when criterion_met=true and evidence_tier=attested', async () => {
    // A cloud run where criterion_met=true but evidence_tier=attested should show
    // "Attested" from EvidenceTierBadge — never the old hardcoded "Machine-verified" label.
    const docCloudCriterionMet = {
      ...computeDoc,
      id: 'doc-provenance-cloud-criterion-met',
      title: 'Cloud experiment criterion met but attested tier',
      criterion_met: true,
      evidence_tier: 'attested' as const,
      forward_quality_verdict: {
        ...computeDoc.forward_quality_verdict,
        terminal_diagnostics: {
          executing_backend: 'gcp',
          runner_target_id: 'cloud-runner-xyz',
        },
      },
    };
    await renderAndOpenDoc(docCloudCriterionMet);
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).toContain('Run provenance');
    expect(text).toContain('Attested');
    expect(text).not.toContain('Machine-verified');
    expect(text).not.toContain('Machine verified');
  });

  test('renders no run provenance panel when terminal_diagnostics is absent', async () => {
    const docWithoutProvenance = {
      ...computeDoc,
      id: 'doc-no-provenance',
      title: 'GPU benchmark without provenance',
      forward_quality_verdict: {
        ...computeDoc.forward_quality_verdict,
        // no terminal_diagnostics
      },
    };
    await renderAndOpenDoc(docWithoutProvenance);
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).not.toContain('Run provenance');
  });

  test('renders no run provenance panel when forward_quality_verdict is null', async () => {
    const docNullVerdict = {
      ...computeDoc,
      id: 'doc-null-verdict',
      title: 'GPU benchmark null verdict',
      forward_quality_verdict: null,
    };
    await renderAndOpenDoc(docNullVerdict as never);
    const text = (host as HTMLDivElement).textContent ?? '';
    expect(text).not.toContain('Run provenance');
  });
});
