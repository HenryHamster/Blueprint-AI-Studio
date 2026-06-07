import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { KnowledgeBrowser } from './KnowledgeBrowser';

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
