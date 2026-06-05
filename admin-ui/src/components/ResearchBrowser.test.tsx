import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ResearchBrowser } from './ResearchBrowser';
import type { Task, ResearchFinding } from '../types/task';
import { fetchRecentFindings } from '../api';

vi.mock('../api', () => ({
  API_BASE_URL: '',
  authHeaders: vi.fn(() => Promise.resolve({})),
  fetchRecentFindings: vi.fn(() => Promise.reject(new Error('no data'))),
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

function makeImplementationTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'impl-1',
    title: 'Implementation task',
    description: 'desc',
    status: 'COMPLETED',
    task_type: 'create',
    created_at: '2026-05-02T00:00:00Z',
    created_by: 'AGENT',
    requires_approval: false,
    context: {
      research_task_id: 'research-1',
      finding_index: 0,
    },
    pr_url: 'https://example.com/pr/1',
    ...overrides,
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

  it('surfaces a child task evaluation summary on the outcome row', async () => {
    const user = userEvent.setup();
    const implementation = makeImplementationTask({
      evaluation_summary: {
        status: 'COMPLETED',
        verdict: 'pass',
        evidence: [
          { label: 'Tests', value: '23 passed, 0 failed' },
        ],
        highlights: [
          { label: 'Top', value: 'full suite green' },
        ],
      },
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Finding A' })]), implementation]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByText('Finding A'));
    const verdictPill = screen.getByText('Pass');
    expect(verdictPill).toBeInTheDocument();
    // Humanized label + positive (emerald) tone, not the raw token or always-cyan.
    expect(verdictPill.className).toContain('emerald');
    expect(verdictPill.className).not.toContain('cyan');
    expect(screen.getByText(/full suite green/)).toBeInTheDocument();
  });

  it('renders a request_changes verdict as a humanized blocking pill', async () => {
    const user = userEvent.setup();
    const implementation = makeImplementationTask({
      task_type: 'panel_review',
      evaluation_summary: {
        status: 'COMPLETED',
        verdict: 'request_changes',
        evidence: [{ label: 'Decision', value: 'REQUEST_CHANGES' }],
        highlights: [{ label: 'Correctness', value: 'fail' }],
      },
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Finding B' })]), implementation]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByText('Finding B'));
    const verdictPill = screen.getByText('Request Changes');
    expect(verdictPill).toBeInTheDocument();
    expect(verdictPill.className).toContain('red');
    expect(screen.queryByText('request_changes')).not.toBeInTheDocument();
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

describe('ResearchBrowser research quality card', () => {
  it('renders grade distribution when API returns findings', async () => {
    vi.mocked(fetchRecentFindings).mockResolvedValueOnce({
      findings: [
        { task_id: 't1', title: 'Fix auth bypass', type: 'bug', scope: 'small', priority: 'high', actionability_grade: 'HIGH', direction_id: null, researched_at: '2026-05-27T10:00:00Z' },
        { task_id: 't2', title: 'Update docs', type: 'improvement', scope: 'small', priority: 'low', actionability_grade: 'LOW', direction_id: null, researched_at: '2026-05-27T09:00:00Z' },
      ],
      grade_distribution: { HIGH: 1, MEDIUM: 0, LOW: 1 },
      grade_distribution_pct: { HIGH: 50, MEDIUM: 0, LOW: 50 },
      total: 2,
    });
    render(
      <ResearchBrowser
        tasks={[]}
        selectedProjectId="proj-1"
        onClose={() => {}}
      />,
    );
    await waitFor(() => {
      expect(screen.getByText('Research quality')).toBeInTheDocument();
    });
    expect(screen.getByText(/1 HIGH/)).toBeInTheDocument();
    expect(screen.getByText(/1 LOW/)).toBeInTheDocument();
    expect(screen.getByText('Fix auth bypass')).toBeInTheDocument();
  });

  it('does not render quality card when API returns no findings', async () => {
    vi.mocked(fetchRecentFindings).mockResolvedValueOnce({
      findings: [],
      grade_distribution: { HIGH: 0, MEDIUM: 0, LOW: 0 },
      grade_distribution_pct: { HIGH: 0, MEDIUM: 0, LOW: 0 },
      total: 0,
    });
    render(
      <ResearchBrowser
        tasks={[]}
        selectedProjectId="proj-1"
        onClose={() => {}}
      />,
    );
    // Wait a tick for the effect to settle, then confirm the card isn't rendered.
    await new Promise<void>(resolve => setTimeout(resolve, 20));
    expect(screen.queryByText('Research quality')).not.toBeInTheDocument();
  });

  it('shows HIGH/MEDIUM/LOW grade chips on recent findings rows', async () => {
    vi.mocked(fetchRecentFindings).mockResolvedValueOnce({
      findings: [
        { task_id: 't1', title: 'High-value finding', type: 'bug', scope: 'medium', priority: 'high', actionability_grade: 'HIGH', direction_id: null, researched_at: null },
        { task_id: 't2', title: 'Medium-value finding', type: 'improvement', scope: 'small', priority: 'medium', actionability_grade: 'MEDIUM', direction_id: null, researched_at: null },
        { task_id: 't3', title: 'Low-value finding', type: 'observation', scope: 'small', priority: 'low', actionability_grade: 'LOW', direction_id: null, researched_at: null },
      ],
      grade_distribution: { HIGH: 1, MEDIUM: 1, LOW: 1 },
      grade_distribution_pct: { HIGH: 33.3, MEDIUM: 33.3, LOW: 33.3 },
      total: 3,
    });
    render(
      <ResearchBrowser
        tasks={[]}
        selectedProjectId="proj-1"
        onClose={() => {}}
      />,
    );
    await waitFor(() => {
      expect(screen.getByText('Research quality')).toBeInTheDocument();
    });
    expect(screen.getByText('HIGH')).toBeInTheDocument();
    expect(screen.getByText('MEDIUM')).toBeInTheDocument();
    expect(screen.getByText('LOW')).toBeInTheDocument();
    expect(screen.getByText('High-value finding')).toBeInTheDocument();
    expect(screen.getByText('Medium-value finding')).toBeInTheDocument();
    expect(screen.getByText('Low-value finding')).toBeInTheDocument();
  });
});

describe('ResearchBrowser researchFeed prop', () => {
  it('uses researchFeed findings instead of tasks prop when provided', () => {
    const taskFinding = makeFinding({ title: 'From tasks prop' });
    const feedFinding = makeFinding({ title: 'From research feed' });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([taskFinding])]}
        researchFeed={[makeResearchTask([feedFinding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText('From research feed')).toBeInTheDocument();
    expect(screen.queryByText('From tasks prop')).not.toBeInTheDocument();
  });

  it('falls back to tasks prop when researchFeed is not provided', () => {
    const taskFinding = makeFinding({ title: 'From tasks prop fallback' });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([taskFinding])]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText('From tasks prop fallback')).toBeInTheDocument();
  });

  it('uses project-wide feed child tasks for outcome state when paginated tasks omit them', async () => {
    const user = userEvent.setup();
    const finding = makeFinding({ title: 'Implemented from off-page task' });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([finding])]}
        researchFeed={[makeResearchTask([finding]), makeImplementationTask()]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Shipped' }));
    expect(screen.getByText('Implemented from off-page task')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Proposed' }));
    expect(screen.queryByText('Implemented from off-page task')).not.toBeInTheDocument();
  });
});
