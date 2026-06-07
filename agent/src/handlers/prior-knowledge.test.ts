import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  strVal: (v: unknown, def = '') => (typeof v === 'string' ? v : def),
  apiGet: vi.fn().mockResolvedValue({ documents: [] }),
}));

// ── Imports under test ────────────────────────────────────────────────────────
import {
  fetchPriorKnowledge,
  buildPriorKnowledgeSection,
  PRIOR_KNOWLEDGE_LIMIT,
  fetchKnowledgeDocumentById,
  buildPrerequisiteKnowledgeSection,
  type PrereqKnowledgeDoc,
} from './prior-knowledge.js';
import { apiGet } from '../index.js';

describe('fetchPriorKnowledge', () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
  });

  it('maps {documents:[...]} into id/title/summary records and hits the agent endpoint with the query', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      documents: [
        { id: 'doc-1', title: 'First doc', summary: 'A summary.', topic: 't', tags: ['x'] },
        { id: 'doc-2', title: 'Second doc', summary: 'Another.' },
      ],
    });

    const docs = await fetchPriorKnowledge('  some query  ');

    expect(docs).toEqual([
      { id: 'doc-1', title: 'First doc', summary: 'A summary.' },
      { id: 'doc-2', title: 'Second doc', summary: 'Another.' },
    ]);
    const calledPath = vi.mocked(apiGet).mock.calls[0][0];
    expect(calledPath).toContain('/api/agent/knowledge-documents');
    expect(calledPath).toContain(`q=${encodeURIComponent('some query')}`);
    expect(calledPath).toContain(`limit=${PRIOR_KNOWLEDGE_LIMIT}`);
  });

  it('returns [] for an empty/whitespace query without calling apiGet', async () => {
    const docs = await fetchPriorKnowledge('   ');
    expect(docs).toEqual([]);
    expect(apiGet).not.toHaveBeenCalled();
  });

  it('returns [] when apiGet rejects (fail-soft)', async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error('unreachable'));
    const docs = await fetchPriorKnowledge('query');
    expect(docs).toEqual([]);
  });

  it('drops malformed entries (missing id or title, non-objects) and caps at the limit', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      documents: [
        null,
        'not-an-object',
        { title: 'No id here', summary: 's' },
        { id: 'no-title', summary: 's' },
        { id: 'good-1', title: 'Good one', summary: '' },
        { id: 'good-2', title: 'Good two', summary: 'x' },
        { id: 'good-3', title: 'Good three', summary: 'x' },
        { id: 'good-4', title: 'Good four', summary: 'x' },
        { id: 'good-5', title: 'Good five', summary: 'x' },
        { id: 'good-6', title: 'Good six', summary: 'x' },
      ],
    });

    const docs = await fetchPriorKnowledge('query');
    expect(docs).toHaveLength(PRIOR_KNOWLEDGE_LIMIT);
    expect(docs.every(d => d.id && d.title)).toBe(true);
    expect(docs[0]).toEqual({ id: 'good-1', title: 'Good one', summary: '' });
  });

  it('returns [] when documents is missing or not an array', async () => {
    vi.mocked(apiGet).mockResolvedValue({});
    expect(await fetchPriorKnowledge('query')).toEqual([]);
    vi.mocked(apiGet).mockResolvedValue({ documents: 'nope' });
    expect(await fetchPriorKnowledge('query')).toEqual([]);
  });
});

describe('buildPriorKnowledgeSection', () => {
  it('returns "" for an empty list', () => {
    expect(buildPriorKnowledgeSection([])).toBe('');
  });

  it('lists ids, titles, and summaries with the build-on/cite instruction', () => {
    const section = buildPriorKnowledgeSection([
      { id: 'doc-1', title: 'First doc', summary: 'A summary.' },
      { id: 'doc-2', title: 'Second doc', summary: '' },
    ]);
    expect(section).toContain('## Prior Knowledge');
    expect(section).toContain('doc-1');
    expect(section).toContain('First doc');
    expect(section).toContain('A summary.');
    expect(section).toContain('doc-2');
    expect(section).toContain('Second doc');
    // Empty summary falls back to a placeholder.
    expect(section).toContain('(no summary)');
    // Build-on / supersede / cite instruction.
    expect(section).toContain('supersede');
    expect(section).toContain('cite');
  });
});

describe('fetchKnowledgeDocumentById', () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
  });

  it('resolves a document (with body) by id and hits the by-id endpoint', async () => {
    vi.mocked(apiGet).mockResolvedValue({
      id: 'doc-1',
      title: 'Commissioned design',
      summary: 'A grounded design.',
      body: '# Design\n\nFull body here.',
    });

    const doc = await fetchKnowledgeDocumentById('doc-1');

    expect(doc).toEqual({
      id: 'doc-1',
      title: 'Commissioned design',
      summary: 'A grounded design.',
      body: '# Design\n\nFull body here.',
    });
    const calledPath = vi.mocked(apiGet).mock.calls[0][0];
    expect(calledPath).toBe('/api/agent/knowledge-documents/doc-1');
  });

  it('returns null for an empty/whitespace id without calling apiGet', async () => {
    expect(await fetchKnowledgeDocumentById('   ')).toBeNull();
    expect(apiGet).not.toHaveBeenCalled();
  });

  it('returns null when apiGet rejects (fail-soft)', async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error('404 not found'));
    expect(await fetchKnowledgeDocumentById('doc-1')).toBeNull();
  });

  it('returns null when the response has no usable id', async () => {
    vi.mocked(apiGet).mockResolvedValue({ title: 'no id' });
    expect(await fetchKnowledgeDocumentById('doc-1')).toBeNull();
  });
});

describe('buildPrerequisiteKnowledgeSection', () => {
  it('returns "" when there are no pointers', () => {
    expect(buildPrerequisiteKnowledgeSection([], new Map())).toBe('');
  });

  it('renders the resolved body and the commissioned framing', () => {
    const resolved = new Map<string, PrereqKnowledgeDoc>([
      ['doc-1', { id: 'doc-1', title: 'Commissioned design', summary: 's', body: 'FULL BODY CONTENT' }],
    ]);
    const section = buildPrerequisiteKnowledgeSection(
      [{ doc_id: 'doc-1', title: 'stamped title', summary: 'stamped summary' }],
      resolved,
    );
    expect(section).toContain('## Commissioned Prerequisite Knowledge');
    expect(section).toContain('produced SPECIFICALLY to inform this task');
    expect(section).toContain('doc-1');
    expect(section).toContain('Commissioned design');
    expect(section).toContain('FULL BODY CONTENT');
  });

  it('falls back to the stamped summary when the body cannot be resolved', () => {
    const section = buildPrerequisiteKnowledgeSection(
      [{ doc_id: 'doc-1', title: 'stamped title', summary: 'stamped summary' }],
      new Map(),
    );
    expect(section).toContain('## Commissioned Prerequisite Knowledge');
    expect(section).toContain('stamped title');
    expect(section).toContain('stamped summary');
    expect(section).toContain('could not be resolved');
  });

  it('skips pointers with an empty doc_id', () => {
    const section = buildPrerequisiteKnowledgeSection(
      [{ doc_id: '', title: 'ignored', summary: 'ignored' }],
      new Map(),
    );
    expect(section).toBe('');
  });
});
