import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
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
    // Humanized label + positive (status-success) tone, not the raw token or always-cyan.
    expect(verdictPill.className).toContain('status-success');
    expect(verdictPill.className).not.toContain('cyan');
    expect(screen.getByText(/full suite green/)).toBeInTheDocument();
  });

  it('does not let keyboard events from nested task actions toggle the finding row', async () => {
    const user = userEvent.setup();
    const onNavigateToTask = vi.fn();
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Keyboard-safe finding' })]), makeImplementationTask()]}
        selectedProjectId={null}
        onClose={() => {}}
        onNavigateToTask={onNavigateToTask}
      />,
    );

    const taskButton = screen.getByTitle('View task (Merged)');
    taskButton.focus();
    await user.keyboard('{Enter}');

    expect(onNavigateToTask).toHaveBeenCalledWith('impl-1');
    expect(screen.queryByText('evidence-bearing finding')).not.toBeInTheDocument();
  });

  it('renders a request_changes verdict as a humanized blocking pill', async () => {
    const user = userEvent.setup();
    const implementation = makeImplementationTask({
      task_type: 'review',
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
    expect(verdictPill.className).toContain('status-blocking');
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

describe('ResearchBrowser spawn-gate provenance', () => {
  afterEach(() => vi.restoreAllMocks());

  it('shows challenged-pass badge for a finding with an explicit challenger pass', () => {
    const impl = makeImplementationTask({
      context: {
        research_task_id: 'research-1',
        finding_index: 0,
        research_spawn_gate_challenged: true,
        research_spawn_gate_verdict: 'pass' as const,
        research_spawn_gate_explicit: true,
        research_spawn_gate_reason: 'Challenger confirmed actionability',
      },
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Gate Finding' })]), impl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    const badge = screen.getByTitle(/Challenger confirmed actionability/);
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toBe('gate ✓');
    expect(badge.className).toContain('emerald');
  });

  it('shows fail-open badge with amber styling for a fail-open pass', () => {
    const impl = makeImplementationTask({
      context: {
        research_task_id: 'research-1',
        finding_index: 0,
        research_spawn_gate_challenged: true,
        research_spawn_gate_verdict: 'pass' as const,
        research_spawn_gate_explicit: false,
        research_spawn_gate_reason: 'fail-open: no verdict returned for this finding',
      },
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Fail-Open Finding' })]), impl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    const badge = screen.getByText('fail-open');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain('amber');
    expect(badge).toHaveAttribute('title', expect.stringContaining('no verdict returned'));
  });

  it('shows refuted badge for a finding deferred by the adversarial gate on the parent task', () => {
    const researchTask = {
      ...makeResearchTask([makeFinding({ title: 'Refuted Finding' })]),
      context: {
        deferred_findings: [{
          title: 'Refuted Finding',
          deferred_reason: 'adversarial_gate',
          adversarial_gate_reason: 'Finding lacks reproduce-on-HEAD evidence',
        }],
      },
    };
    render(
      <ResearchBrowser
        tasks={[researchTask]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    const badge = screen.getByText('refuted');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain('red');
    expect(badge).toHaveAttribute('title', expect.stringContaining('reproduce-on-HEAD'));
  });

  it('does not show a gate badge for an unchallenged finding (no gate context)', () => {
    const impl = makeImplementationTask({
      context: {
        research_task_id: 'research-1',
        finding_index: 0,
        // no research_spawn_gate_challenged key
      },
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Unchallenged Finding' })]), impl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByText('gate ✓')).not.toBeInTheDocument();
    expect(screen.queryByText('fail-open')).not.toBeInTheDocument();
    expect(screen.queryByText('refuted')).not.toBeInTheDocument();
  });

  it('does not show refuted badge for a finding deferred for a non-gate reason', () => {
    const researchTask = {
      ...makeResearchTask([makeFinding({ title: 'Low Actionability Finding' })]),
      context: {
        deferred_findings: [{
          title: 'Low Actionability Finding',
          deferred_reason: 'low_actionability',
          adversarial_gate_reason: null,
        }],
      },
    };
    render(
      <ResearchBrowser
        tasks={[researchTask]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    // Only adversarial_gate deferrals show the refuted badge
    expect(screen.queryByText('refuted')).not.toBeInTheDocument();
  });

  it('prefers hash-based deferred matching when finding_hash is present on both sides', () => {
    // The finding's title was edited after the deferral was recorded, so a
    // title-only match would MISS. The stable finding_hash still links them.
    const researchTask = {
      ...makeResearchTask([makeFinding({ title: 'Renamed Finding Title', finding_hash: 'abc123' })]),
      context: {
        deferred_findings: [{
          title: 'Original Stale Title',
          finding_hash: 'abc123',
          deferred_reason: 'adversarial_gate',
          adversarial_gate_reason: 'Refuted by hash-matched challenger',
        }],
      },
    };
    render(
      <ResearchBrowser
        tasks={[researchTask]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    const badge = screen.getByText('refuted');
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute('title', expect.stringContaining('hash-matched'));
  });

  it('falls back to title matching when the finding has no finding_hash', () => {
    const researchTask = {
      ...makeResearchTask([makeFinding({ title: 'No Hash Finding' })]),
      context: {
        deferred_findings: [{
          title: 'No Hash Finding',
          finding_hash: 'xyz789',
          deferred_reason: 'adversarial_gate',
          adversarial_gate_reason: 'Refuted via title fallback',
        }],
      },
    };
    render(
      <ResearchBrowser
        tasks={[researchTask]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    const badge = screen.getByText('refuted');
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute('title', expect.stringContaining('title fallback'));
  });
});

describe('ResearchBrowser BLOCKED/PARKED outcome styling', () => {
  it('renders BLOCKED implementation with amber color and Blocked label', async () => {
    const user = userEvent.setup();
    const blockedImpl = makeImplementationTask({ status: 'BLOCKED' });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Blocked Finding' })]), blockedImpl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByText('Blocked Finding'));
    const label = screen.getByText('Blocked');
    expect(label).toBeInTheDocument();
    expect(label.className).toContain('amber');
  });

  it('renders PARKED implementation with purple color and Parked label', async () => {
    const user = userEvent.setup();
    const parkedImpl = makeImplementationTask({ id: 'impl-2', status: 'PARKED' });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Parked Finding' })]), parkedImpl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByText('Parked Finding'));
    const label = screen.getByText('Parked');
    expect(label).toBeInTheDocument();
    expect(label.className).toContain('purple');
  });

  it('BLOCKED and PARKED implementations appear under In Progress filter', async () => {
    const user = userEvent.setup();
    const blockedImpl = makeImplementationTask({
      id: 'blocked-1',
      status: 'BLOCKED',
      context: { research_task_id: 'research-1', finding_index: 0 },
    });
    const parkedImpl = makeImplementationTask({
      id: 'parked-1',
      status: 'PARKED',
      context: { research_task_id: 'research-1', finding_index: 1 },
    });
    const research = {
      ...makeResearchTask([
        makeFinding({ title: 'Finding Blocked' }),
        makeFinding({ title: 'Finding Parked' }),
      ]),
    };
    render(
      <ResearchBrowser
        tasks={[research, blockedImpl, parkedImpl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'In Progress' }));
    expect(screen.getByText('Finding Blocked')).toBeInTheDocument();
    expect(screen.getByText('Finding Parked')).toBeInTheDocument();
  });
});

describe('ResearchBrowser quality card interactivity', () => {
  it('grade filter narrows the listed findings to the selected grade', async () => {
    const user = userEvent.setup();
    vi.mocked(fetchRecentFindings).mockResolvedValueOnce({
      findings: [
        { task_id: 't1', title: 'High finding', type: 'bug', scope: 'small', priority: 'high', actionability_grade: 'HIGH', direction_id: null, researched_at: null },
        { task_id: 't2', title: 'Low finding', type: 'observation', scope: 'small', priority: 'low', actionability_grade: 'LOW', direction_id: null, researched_at: null },
      ],
      grade_distribution: { HIGH: 1, MEDIUM: 0, LOW: 1 },
      grade_distribution_pct: { HIGH: 50, MEDIUM: 0, LOW: 50 },
      total: 2,
    });
    render(
      <ResearchBrowser tasks={[]} selectedProjectId="proj-1" onClose={() => {}} />,
    );
    await waitFor(() => expect(screen.getByText('Research quality')).toBeInTheDocument());

    // Both findings visible initially
    expect(screen.getByText('High finding')).toBeInTheDocument();
    expect(screen.getByText('Low finding')).toBeInTheDocument();

    // Click the HIGH grade-filter chip. Both the filter chip ("1 HIGH (50%)")
    // and the finding row ("HIGH High finding") expose "HIGH" in their
    // accessible name, so scope by the chip's distribution-percentage text.
    const highFilterChip = () => screen.getByRole('button', { name: /HIGH \(\d+%\)/ });
    await user.click(highFilterChip());

    // Only HIGH finding visible; LOW hidden
    expect(screen.getByText('High finding')).toBeInTheDocument();
    expect(screen.queryByText('Low finding')).not.toBeInTheDocument();

    // Clicking again clears the filter
    await user.click(highFilterChip());
    expect(screen.getByText('Low finding')).toBeInTheDocument();
  });

  it('clicking a quality card row expands that finding\'s detail in the main list', async () => {
    const user = userEvent.setup();
    const finding = makeFinding({ title: 'Actionable finding', description: 'important detail here' });
    const researchTask = makeResearchTask([finding]);
    vi.mocked(fetchRecentFindings).mockResolvedValueOnce({
      findings: [
        { task_id: 'research-1', title: 'Actionable finding', type: 'bug', scope: 'small', priority: 'high', actionability_grade: 'HIGH', direction_id: null, researched_at: null },
      ],
      grade_distribution: { HIGH: 1, MEDIUM: 0, LOW: 0 },
      grade_distribution_pct: { HIGH: 100, MEDIUM: 0, LOW: 0 },
      total: 1,
    });
    render(
      <ResearchBrowser
        tasks={[researchTask]}
        selectedProjectId="proj-1"
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(screen.getByText('Research quality')).toBeInTheDocument());

    // Description not visible yet
    expect(screen.queryByText('important detail here')).not.toBeInTheDocument();

    // Click the finding row in the quality card. The same title also renders as
    // a row in the main findings list, so scope the query to the quality card
    // (the container holding the "Research quality" header) to target the right one.
    const qualityCard = screen.getByText('Research quality').closest('div') as HTMLElement;
    await user.click(within(qualityCard).getByRole('button', { name: /Actionable finding/ }));

    // Finding detail (description) is now expanded
    await waitFor(() => expect(screen.getByText('important detail here')).toBeInTheDocument());
  });
});

describe('ResearchBrowser actionable findings', () => {
  function makeRefutedResearchTask(): Task {
    return {
      ...makeResearchTask([
        makeFinding({ title: 'Open proposal' }),
        makeFinding({ title: 'Already shipped' }),
        makeFinding({ title: 'Just a note', type: 'direction_note' }),
        makeFinding({ title: 'Refuted proposal' }),
      ]),
      context: {
        deferred_findings: [{
          title: 'Refuted proposal',
          deferred_reason: 'adversarial_gate',
          adversarial_gate_reason: 'No reproduce-on-HEAD evidence',
        }],
      },
    };
  }

  it('Actionable filter shows only unimplemented, non-metadata, non-refuted findings', async () => {
    const user = userEvent.setup();
    const shippedImpl = makeImplementationTask({
      context: { research_task_id: 'research-1', finding_index: 1 },
    });
    render(
      <ResearchBrowser
        tasks={[makeRefutedResearchTask(), shippedImpl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Actionable' }));

    // Only the open, unrefuted, non-metadata proposal survives.
    expect(screen.getByText('Open proposal')).toBeInTheDocument();
    expect(screen.queryByText('Already shipped')).not.toBeInTheDocument();
    expect(screen.queryByText('Just a note')).not.toBeInTheDocument();
    expect(screen.queryByText('Refuted proposal')).not.toBeInTheDocument();
  });

  it('group header shows an actionable count distinct from the proposed count', () => {
    const shippedImpl = makeImplementationTask({
      context: { research_task_id: 'research-1', finding_index: 1 },
    });
    render(
      <ResearchBrowser
        tasks={[makeRefutedResearchTask(), shippedImpl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    // One genuinely actionable proposal — the shipped finding, the refuted one,
    // and the metadata note are all excluded — surfaced as an emphasized chip.
    const chip = screen.getByText(/1 actionable/);
    expect(chip).toBeInTheDocument();
    expect(chip.className).toContain('cyan');
  });

  it('does not show an actionable chip when every finding is implemented', () => {
    const shippedImpl = makeImplementationTask({
      context: { research_task_id: 'research-1', finding_index: 0 },
    });
    render(
      <ResearchBrowser
        tasks={[makeResearchTask([makeFinding({ title: 'Done finding' })]), shippedImpl]}
        selectedProjectId={null}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByText(/actionable/)).not.toBeInTheDocument();
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

describe('ResearchBrowser routed direction filter', () => {
  it('updates the visible direction filter when the routed prop changes while open', () => {
    const { rerender } = render(
      <ResearchBrowser
        tasks={[]}
        selectedProjectId={null}
        initialDirectionId="alpha-route"
        onClose={() => {}}
      />,
    );

    expect(screen.getByText('Direction alpha-ro')).toBeInTheDocument();

    rerender(
      <ResearchBrowser
        tasks={[]}
        selectedProjectId={null}
        initialDirectionId="beta-route"
        onClose={() => {}}
      />,
    );

    expect(screen.getByText('Direction beta-rou')).toBeInTheDocument();
    expect(screen.queryByText('Direction alpha-ro')).not.toBeInTheDocument();
  });
});
