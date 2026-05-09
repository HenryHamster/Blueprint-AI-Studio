import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ResearchBrowser } from './ResearchBrowser';
import type { Task, ResearchFinding } from '../types/task';

vi.mock('../api', () => ({
  API_BASE_URL: '',
  authHeaders: vi.fn(() => Promise.resolve({})),
}));

beforeEach(() => {
  // Stub fetch for the directions endpoint — return ok:false so it bails harmlessly.
  // Cast through unknown to satisfy the global fetch signature in test scope.
  globalThis.fetch = vi.fn(() => Promise.resolve({ ok: false, json: () => Promise.resolve({}) })) as unknown as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
});

function makeFinding(overrides: Partial<ResearchFinding> = {}): ResearchFinding {
  return {
    title: 'Finding A',
    description: 'evidence-bearing finding',
    prompt: 'do X',
    scope: 'small',
    risk: 'low',
    priority: 'medium',
    related_files: [],
    ...overrides,
  };
}

function makeResearchTask(findings: ResearchFinding[]): Task {
  return {
    id: 'research-1',
    title: 'Research task',
    description: 'desc',
    status: 'COMPLETED',
    task_type: 'research',
    created_at: '2026-05-01T00:00:00Z',
    created_by: 'AGENT',
    requires_approval: false,
    research_findings: findings,
  };
}

describe('ResearchBrowser evidence panel', () => {
  it('renders claims with show-more toggle and source refs', async () => {
    const user = userEvent.setup();
    const finding = makeFinding({
      claims: ['claim a', 'claim b', 'claim c', 'claim d'],
      sources: [
        { type: 'file', ref: 'agent/src/foo.ts:42' },
        { type: 'pr', ref: 'pr#3874' },
        { type: 'log', ref: 'https://example.com/log' },
      ],
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([finding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByText('Finding A'));

    // First 3 claims visible; the 4th hidden behind toggle.
    expect(screen.getByText('claim a')).toBeInTheDocument();
    expect(screen.getByText('claim b')).toBeInTheDocument();
    expect(screen.getByText('claim c')).toBeInTheDocument();
    expect(screen.queryByText('claim d')).not.toBeInTheDocument();

    await user.click(screen.getByText(/^Show 1 more$/));
    expect(screen.getByText('claim d')).toBeInTheDocument();

    // External https URL renders as anchor with target=_blank.
    const link = screen.getByRole('link', { name: 'https://example.com/log' });
    expect(link).toHaveAttribute('href', 'https://example.com/log');
    expect(link).toHaveAttribute('target', '_blank');

    // pr#3874 has no repo_url to pin against — renders as monospaced text.
    expect(screen.getByText('pr#3874')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'pr#3874' })).toBeNull();

    // File path also has no repo_url — renders as text.
    expect(screen.getByText('agent/src/foo.ts:42')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'agent/src/foo.ts:42' })).toBeNull();
  });

  it('renders broken-refs badge when finding has broken_source_ref_count > 0', () => {
    const finding = makeFinding({
      title: 'Hallucinated finding',
      _provenance_warnings: {
        broken_source_ref_count: 2,
        broken_refs: ['agent/src/missing.ts', 'backend/app/gone.py'],
      },
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([finding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    const badge = screen.getByText(/2 broken refs/);
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute('title', expect.stringContaining('agent/src/missing.ts'));
    expect(badge).toHaveAttribute('title', expect.stringContaining('backend/app/gone.py'));
  });

  it('does not render broken-refs badge when no provenance warnings', () => {
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Clean finding' })])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText('Clean finding')).toBeInTheDocument();
    expect(screen.queryByText(/broken refs/)).not.toBeInTheDocument();
  });

  it('does not render broken-refs badge when broken_source_ref_count is 0', () => {
    const finding = makeFinding({
      title: 'Zero-count finding',
      _provenance_warnings: { broken_source_ref_count: 0, broken_refs: [] },
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([finding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByText(/broken refs/)).not.toBeInTheDocument();
  });

  it('renders nothing when finding has no claims and no sources', async () => {
    const user = userEvent.setup();
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Plain finding' })])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByText('Plain finding'));
    expect(screen.queryByText(/^Claims$/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Sources$/i)).not.toBeInTheDocument();
  });
});

describe('ResearchBrowser findings_documented filter', () => {
  it('shows metadata findings only under Documented filter', async () => {
    const user = userEvent.setup();
    const metaFinding = makeFinding({ title: 'Design observation', type: 'design_doc_observation' });
    const implFinding = makeFinding({ title: 'Implementation finding' });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([metaFinding, implFinding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    // Before filtering: only implementation findings are shown (All filter excludes metadata).
    expect(screen.getByText('Implementation finding')).toBeInTheDocument();
    expect(screen.queryByText('Design observation')).not.toBeInTheDocument();

    // Switch to Documented filter.
    await user.click(screen.getByRole('button', { name: 'Documented' }));
    expect(screen.getByText('Design observation')).toBeInTheDocument();
    expect(screen.queryByText('Implementation finding')).not.toBeInTheDocument();
  });

  it('shows direction_note and direction_proposal findings under Documented filter', async () => {
    const user = userEvent.setup();
    const noteFinding = makeFinding({ title: 'Direction note', type: 'direction_note' });
    const proposalFinding = makeFinding({ title: 'Direction proposal', type: 'direction_proposal' });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([noteFinding, proposalFinding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Documented' }));
    expect(screen.getByText('Direction note')).toBeInTheDocument();
    expect(screen.getByText('Direction proposal')).toBeInTheDocument();
  });

  it('metadata findings not shown in Proposed filter (backward compatible)', async () => {
    const user = userEvent.setup();
    const metaFinding = makeFinding({ title: 'Meta only', type: 'direction_note' });
    const implFinding = makeFinding({ title: 'Impl proposed' });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([metaFinding, implFinding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Proposed' }));
    // Only implementation findings without a child task appear under Proposed.
    expect(screen.getByText('Impl proposed')).toBeInTheDocument();
    expect(screen.queryByText('Meta only')).not.toBeInTheDocument();
  });
});
