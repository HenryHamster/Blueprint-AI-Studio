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

vi.mock('../api', () => ({
  API_BASE_URL: '',
  authGet: vi.fn(() => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) })),
  authHeaders: vi.fn(() => Promise.resolve({})),
  fetchKnowledgeDocuments: vi.fn(() =>
    Promise.resolve({ documents: [webResearchDoc], total: 1, limit: 50, offset: 0 }),
  ),
  fetchKnowledgeDocument: vi.fn(() =>
    Promise.resolve({ ...webResearchDoc, body: '# Body\n\nMarkdown body.' }),
  ),
  resolveArtifactDownloadUrl: vi.fn(() => Promise.resolve('https://signed.example.com/knowledge/doc-web-research-grounded')),
}));

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
    expect(measuredCard && findBadge(measuredCard, 'Measured only')).toBeTruthy();
    expect(measuredCard?.textContent).toContain('Compute');

    // Open the acceptance-grade document — its badge also renders in the detail meta row.
    if (!acceptanceCard) throw new Error('expected acceptance compute card');
    await act(async () => { (acceptanceCard as HTMLElement).click(); });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    expect(findBadge(host as HTMLDivElement, 'Acceptance')).toBeTruthy();
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
