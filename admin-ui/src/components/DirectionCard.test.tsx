import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DirectionCard } from './DirectionCard';
import type { Direction } from './DirectionsPanel';

function makeDirection(overrides: Partial<Direction> = {}): Direction {
  return {
    id: 'd-1',
    text: 'Improve pipeline reliability',
    priority: 'high',
    status: 'active',
    added_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeProps(direction: Direction, statusKey = 'active') {
  return {
    direction,
    statusKey,
    activeItems: [direction],
    editingId: null,
    editText: '',
    editPriority: 'medium' as const,
    editStatus: 'active' as const,
    editCategory: null,
    editPathTags: [],
    editPathInput: '',
    editCompletionCriteria: '',
    editSuccessEvidence: '',
    editRetirementCondition: '',
    setEditingId: vi.fn(),
    setEditText: vi.fn(),
    setEditPriority: vi.fn(),
    setEditStatus: vi.fn(),
    setEditCategory: vi.fn(),
    setEditPathTags: vi.fn(),
    setEditPathInput: vi.fn(),
    setEditCompletionCriteria: vi.fn(),
    setEditSuccessEvidence: vi.fn(),
    setEditRetirementCondition: vi.fn(),
    startEdit: vi.fn(),
    updateDirection: vi.fn(),
    approveProposal: vi.fn(),
    rejectProposal: vi.fn(),
    reviveProposal: vi.fn(),
    reactivateDirection: vi.fn(),
    activateStagedDirection: vi.fn(),
    cancelStagedDirection: vi.fn(),
    unholdDirection: vi.fn(),
    markDirectionComplete: vi.fn(),
    quickToggleStatus: vi.fn(),
    deleteDirection: vi.fn(),
    applyDirectionUpdate: vi.fn(),
    dismissDirectionUpdate: vi.fn(),
    triggerResearch: vi.fn(),
    reorderDirections: vi.fn(),
    taskCounts: {},
    activeTasks: {},
    cooldownMinutes: 240,
    researchingIds: {},
    researchSuccessIds: {},
    forceConfirmId: null,
    setForceConfirmId: vi.fn(),
  };
}

describe('DirectionCard diagnosis/remediation section', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-24T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders held banner with reason, auto-recovery text, and unhold action', () => {
    const d = makeDirection({
      held: true,
      held_reason: 'Held: direction has 3 consecutive CODE_ERROR failures — awaiting investigation',
      held_until: '2026-04-24T13:23:00Z',
    });
    const props = makeProps(d);

    render(<DirectionCard {...props} />);

    expect(screen.getByText('On hold')).toBeInTheDocument();
    expect(screen.getByText(/Held: direction has 3 consecutive CODE_ERROR failures/)).toBeInTheDocument();
    expect(screen.getByText('auto-recovers in 1h 23m')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'unhold' }));
    expect(props.unholdDirection).toHaveBeenCalledWith('d-1');
  });

  it('renders overdue text when held_until is in the past', () => {
    const d = makeDirection({
      held: true,
      held_reason: 'Held after repeated failures.',
      held_until: '2026-04-24T11:59:00Z',
    });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('auto-recovery overdue')).toBeInTheDocument();
  });

  it('does not render hold banner for unheld directions', () => {
    const d = makeDirection({ held: false, held_reason: null });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText('On hold')).not.toBeInTheDocument();
  });

  it('renders manual-intervention alert when needs_manual_intervention is true', () => {
    const d = makeDirection({
      needs_manual_intervention: true,
      remediation_attempts: 3,
      diagnosis_summary: 'Repeated review_quality failures on review-fix agent.',
    });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('Manual intervention needed')).toBeInTheDocument();
    expect(screen.getByText(/Auto-remediation escalated after 3 failed attempts/)).toBeInTheDocument();
    expect(screen.getByText(/Repeated review_quality failures/)).toBeInTheDocument();
  });

  it('renders in-progress indicator when diagnosis_in_progress is true', () => {
    const d = makeDirection({ diagnosis_in_progress: true });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('Diagnosing failure pattern…')).toBeInTheDocument();
  });

  it('renders diagnosis summary when present and not in progress', () => {
    const d = makeDirection({
      diagnosis_summary: 'Root cause: agent timing out on force-with-lease retry.',
      diagnosis_completed_at: '2024-01-02T00:00:00Z',
    });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText(/Root cause: agent timing out/)).toBeInTheDocument();
    expect(screen.getByText(/Diagnosis:/)).toBeInTheDocument();
  });

  it('hides the completed diagnosis summary while a new diagnosis is in progress', () => {
    const d = makeDirection({
      diagnosis_in_progress: true,
      diagnosis_summary: 'Stale summary from a prior cycle.',
    });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('Diagnosing failure pattern…')).toBeInTheDocument();
    expect(screen.queryByText(/Stale summary/)).not.toBeInTheDocument();
  });

  it('renders remediation attempt badge with merged result in emerald', () => {
    const d = makeDirection({
      remediation_attempts: 1,
      last_remediation_result: 'merged',
    });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText(/Remediation 1× · merged/);
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/emerald/);
  });

  it('renders remediation attempt badge with failed result in red', () => {
    const d = makeDirection({
      remediation_attempts: 2,
      last_remediation_result: 'failed',
    });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText(/Remediation 2× · failed/);
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/red/);
  });

  it('renders remediation history summary and show-more expansion', () => {
    const d = makeDirection({
      remediation_history: [
        {
          task_id: 't-3',
          result: 'failed',
          fingerprint: 'cccccccc33333333',
          timestamp: '2026-04-24T10:00:00Z',
        },
        {
          task_id: 't-2',
          result: 'partial_success',
          fingerprint: 'bbbbbbbb22222222',
          timestamp: '2026-04-24T11:00:00Z',
        },
        {
          task_id: 't-1',
          result: 'merged',
          fingerprint: 'aaaaaaaa11111111',
          timestamp: '2026-04-24T11:30:00Z',
        },
      ],
    });

    render(<DirectionCard {...makeProps(d)} />);

    expect(screen.getByText('Recent remediation attempts')).toBeInTheDocument();
    expect(screen.getByText('merged')).toBeInTheDocument();
    expect(screen.getByText('aaaaaaaa')).toBeInTheDocument();
    expect(screen.getByText('partial')).toBeInTheDocument();
    expect(screen.getByText('bbbbbbbb')).toBeInTheDocument();
    expect(screen.queryByText('cccccccc')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'show more' }));
    expect(screen.getByText('cccccccc')).toBeInTheDocument();
  });

  it('does not render remediation history section when history is missing', () => {
    const d = makeDirection();
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText('Recent remediation attempts')).not.toBeInTheDocument();
  });

  it('does not render remediation history section when history is empty', () => {
    const d = makeDirection({ remediation_history: [] });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText('Recent remediation attempts')).not.toBeInTheDocument();
  });

  it('renders completion_criteria textarea in edit mode and propagates changes', () => {
    const d = makeDirection();
    const setEditCompletionCriteria = vi.fn();
    const props = {
      ...makeProps(d),
      editingId: d.id,
      editText: d.text,
      editCompletionCriteria: 'Merged across backend/',
      setEditCompletionCriteria,
    };
    render(<DirectionCard {...props} />);
    const textarea = screen.getByPlaceholderText('Done when...') as HTMLTextAreaElement;
    expect(textarea).toBeInTheDocument();
    expect(textarea.value).toBe('Merged across backend/');
    fireEvent.change(textarea, { target: { value: 'All tests passing' } });
    expect(setEditCompletionCriteria).toHaveBeenCalledWith('All tests passing');
  });

  it('renders acceptance-evidence textareas in edit mode and propagates changes', () => {
    const d = makeDirection();
    const setEditSuccessEvidence = vi.fn();
    const setEditRetirementCondition = vi.fn();
    const props = {
      ...makeProps(d),
      editingId: d.id,
      editText: d.text,
      editSuccessEvidence: 'p99 < 200ms',
      editRetirementCondition: 'one clean cycle',
      setEditSuccessEvidence,
      setEditRetirementCondition,
    };
    render(<DirectionCard {...props} />);
    const success = screen.getByPlaceholderText('Success evidence...') as HTMLTextAreaElement;
    const retirement = screen.getByPlaceholderText('Retirement condition...') as HTMLTextAreaElement;
    expect(success.value).toBe('p99 < 200ms');
    expect(retirement.value).toBe('one clean cycle');
    fireEvent.change(success, { target: { value: 'latency under target' } });
    expect(setEditSuccessEvidence).toHaveBeenCalledWith('latency under target');
    fireEvent.change(retirement, { target: { value: 'promotion verified' } });
    expect(setEditRetirementCondition).toHaveBeenCalledWith('promotion verified');
  });

  it('shows retirement_condition from context in view mode', () => {
    const d = makeDirection({ context: { retirement_condition: 'Retire after one promotion cycle' } });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('Retirement condition:')).toBeInTheDocument();
    expect(screen.getByText('Retire after one promotion cycle')).toBeInTheDocument();
  });

  it('renders no diagnosis section when all diagnosis fields are absent', () => {
    const d = makeDirection();
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText('Manual intervention needed')).not.toBeInTheDocument();
    expect(screen.queryByText('Diagnosing failure pattern…')).not.toBeInTheDocument();
    expect(screen.queryByText(/Diagnosis:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Remediation/)).not.toBeInTheDocument();
  });
});

describe('DirectionCard quality score badge', () => {
  it('renders green badge when quality_score >= 0.6', () => {
    const d = makeDirection({ quality_score: 0.75 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('75% quality');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/emerald/);
  });

  it('renders amber badge when quality_score >= 0.3 and < 0.6', () => {
    const d = makeDirection({ quality_score: 0.4 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('40% quality');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/amber/);
  });

  it('renders red badge when quality_score < 0.3', () => {
    const d = makeDirection({ quality_score: 0.2 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('20% quality');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/red/);
  });

  it('hides badge when quality_score is absent', () => {
    const d = makeDirection();
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText(/quality/)).not.toBeInTheDocument();
  });

  it('hides badge when quality_score is null', () => {
    const d = makeDirection({ quality_score: null });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText(/quality/)).not.toBeInTheDocument();
  });

  it('renders at threshold: 0.6 is green, 0.3 is amber', () => {
    const { rerender } = render(<DirectionCard {...makeProps(makeDirection({ quality_score: 0.6 }))} />);
    expect(screen.getByText('60% quality').className).toMatch(/emerald/);
    rerender(<DirectionCard {...makeProps(makeDirection({ quality_score: 0.3 }))} />);
    expect(screen.getByText('30% quality').className).toMatch(/amber/);
  });
});

describe('DirectionCard paused reason display', () => {
  it('shows amber text for yield/saturation pause reason', () => {
    const d = makeDirection({
      status: 'paused',
      paused_reason: 'Saturated: last 5 research cycles produced no actionable findings',
      paused_at: '2026-04-24T10:00:00Z',
      auto_paused: true,
    });
    render(<DirectionCard {...makeProps(d, 'paused')} />);
    const el = screen.getByText(/Saturated: last 5 research cycles/);
    expect(el).toBeInTheDocument();
    expect(el.className).toMatch(/amber/);
    expect(el.className).not.toMatch(/red/);
  });

  it('shows red text for budget-exceeded pause reason', () => {
    const d = makeDirection({
      status: 'paused',
      paused_reason: 'Budget exceeded: $6.00 spent of $5.00 limit',
      paused_at: '2026-04-24T10:00:00Z',
      auto_paused: true,
      budget_exceeded: true,
    });
    render(<DirectionCard {...makeProps(d, 'paused')} />);
    const el = screen.getByText(/Budget exceeded:/);
    expect(el).toBeInTheDocument();
    expect(el.className).toMatch(/red/);
    expect(el.className).not.toMatch(/amber/);
  });

  it('does not render paused reason subtitle when paused_reason is absent', () => {
    const d = makeDirection({ status: 'paused' });
    render(<DirectionCard {...makeProps(d, 'paused')} />);
    // No amber/red reason subtitle should appear — only the status toggle button exists
    expect(screen.queryByText(/Saturated/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Budget exceeded/)).not.toBeInTheDocument();
  });
});

describe('DirectionCard budget utilization indicator', () => {
  it('renders nothing when both spend and budget are absent', () => {
    const d = makeDirection();
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText(/spent/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Budget/)).not.toBeInTheDocument();
    expect(screen.queryByText(/over budget/)).not.toBeInTheDocument();
  });

  it('renders spend-only badge when spend is present but no budget', () => {
    const d = makeDirection({ estimated_cost_usd: 1.23 });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('$1.23 spent')).toBeInTheDocument();
  });

  it('renders budget-only badge when budget is present but no spend', () => {
    const d = makeDirection({ cost_budget_usd: 5.00 });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('Budget $5.00')).toBeInTheDocument();
  });

  it('renders emerald badge with percentage when utilization is low (<80%)', () => {
    const d = makeDirection({ estimated_cost_usd: 1.00, cost_budget_usd: 5.00, budget_utilization_pct: 20 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('$1.00 / $5.00 · 20%');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/emerald/);
  });

  it('renders amber badge when utilization is high (>80%) but not exceeded', () => {
    const d = makeDirection({ estimated_cost_usd: 4.50, cost_budget_usd: 5.00, budget_utilization_pct: 90 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('$4.50 / $5.00 · 90%');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/amber/);
  });

  it('renders red over-budget badge when budget_exceeded is true', () => {
    const d = makeDirection({ estimated_cost_usd: 6.00, cost_budget_usd: 5.00, budget_exceeded: true });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('$6.00 · over budget');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/red/);
  });
});

describe('DirectionCard cost-per-merge retirement warning', () => {
  it('renders nothing when cost_per_merge_exceeds_threshold is false', () => {
    const d = makeDirection({ cost_per_merge_exceeds_threshold: false, cost_per_merged_pr: 1.50 });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText('High cost / autonomous merge')).not.toBeInTheDocument();
  });

  it('renders nothing when cost_per_merge_exceeds_threshold is absent', () => {
    const d = makeDirection();
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText('High cost / autonomous merge')).not.toBeInTheDocument();
  });

  it('renders amber warning badge when cost_per_merge_exceeds_threshold is true', () => {
    const d = makeDirection({ cost_per_merge_exceeds_threshold: true, cost_per_merged_pr: 45.00 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('High cost / autonomous merge');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/amber/);
  });

  it('badge title names the autonomous-only denominator', () => {
    const d = makeDirection({ cost_per_merge_exceeds_threshold: true, cost_per_merged_pr: 45.00 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('High cost / autonomous merge');
    expect(badge.getAttribute('title')).toMatch(/Cost per autonomous merge: \$45\.00/);
    expect(badge.getAttribute('title')).toMatch(/autonomous merges only/);
  });

  it('renders provider/model breakdown from retirement_consideration_evidence', () => {
    const d = makeDirection({
      cost_per_merge_exceeds_threshold: true,
      cost_per_merged_pr: 45.00,
      retirement_consideration_evidence: {
        window_cycles: 6,
        direction_cost_per_merge_usd: 45.0,
        threshold_usd: 15.0,
        cost_by_provider_model: {
          'anthropic|claude-opus-4-7': { cost_usd: 36.0, share_pct: 0.8 },
          'openai|gpt-5.4': { cost_usd: 9.0, share_pct: 0.2 },
        },
      },
    });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('High cost / autonomous merge')).toBeInTheDocument();
    // Top entry rendered with model label and percentage share.
    expect(screen.getByText(/claude-opus-4-7 80%/)).toBeInTheDocument();
    expect(screen.getByText(/gpt-5\.4 20%/)).toBeInTheDocument();
  });

  it('falls back to top-level cost_by_provider_model when evidence breakdown absent', () => {
    const d = makeDirection({
      cost_per_merge_exceeds_threshold: true,
      cost_per_merged_pr: 45.00,
      cost_by_provider_model: {
        'anthropic|claude-opus-4-7': 30.0,
        'openai|gpt-5.4': 10.0,
      },
    });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('High cost / autonomous merge')).toBeInTheDocument();
    // 30/40 = 75%, 10/40 = 25%.
    expect(screen.getByText(/claude-opus-4-7 75%/)).toBeInTheDocument();
    expect(screen.getByText(/gpt-5\.4 25%/)).toBeInTheDocument();
  });

  it('renders only the badge (no breakdown) when neither split source is provided', () => {
    const d = makeDirection({ cost_per_merge_exceeds_threshold: true, cost_per_merged_pr: 45.00 });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('High cost / autonomous merge')).toBeInTheDocument();
    // No model labels should appear when there is no breakdown data.
    expect(screen.queryByText(/claude-opus-4-7/)).not.toBeInTheDocument();
    expect(screen.queryByText(/gpt-5\.4/)).not.toBeInTheDocument();
  });
});

describe('DirectionCard per-cycle delta badge', () => {
  it('shows "N this cycle" badge with no delta when only one cycle available', () => {
    const d = makeDirection({
      research_history: [makeResearchEntry({ prs_merged: 3 })],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    expect(screen.getByText('3 this cycle')).toBeInTheDocument();
    expect(screen.queryByText(/vs prior/)).not.toBeInTheDocument();
  });

  it('shows green "+N vs prior" badge when latest cycle improved', () => {
    const d = makeDirection({
      research_history: [
        makeResearchEntry({ prs_merged: 1, timestamp: '2026-01-01T00:00:00Z' }),
        makeResearchEntry({ prs_merged: 3, timestamp: '2026-01-02T00:00:00Z' }),
      ],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    const badge = screen.getByText('+2 vs prior');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/emerald/);
  });

  it('shows red "-N vs prior" badge when latest cycle regressed', () => {
    const d = makeDirection({
      research_history: [
        makeResearchEntry({ prs_merged: 3, timestamp: '2026-01-01T00:00:00Z' }),
        makeResearchEntry({ prs_merged: 1, timestamp: '2026-01-02T00:00:00Z' }),
      ],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    const badge = screen.getByText('-2 vs prior');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/red/);
  });

  it('shows neutral "0 vs prior" badge when latest cycle matches prior', () => {
    const d = makeDirection({
      research_history: [
        makeResearchEntry({ prs_merged: 2, timestamp: '2026-01-01T00:00:00Z' }),
        makeResearchEntry({ prs_merged: 2, timestamp: '2026-01-02T00:00:00Z' }),
      ],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    const badge = screen.getByText('0 vs prior');
    expect(badge).toBeInTheDocument();
    expect(badge.className).not.toMatch(/emerald/);
    expect(badge.className).not.toMatch(/red/);
  });

  it('shows rolling avg label when three or more cycles are available', () => {
    const d = makeDirection({
      research_history: [
        makeResearchEntry({ prs_merged: 2, timestamp: '2026-01-01T00:00:00Z' }),
        makeResearchEntry({ prs_merged: 4, timestamp: '2026-01-02T00:00:00Z' }),
        makeResearchEntry({ prs_merged: 3, timestamp: '2026-01-03T00:00:00Z' }),
      ],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    // avg of last 3: (2+4+3)/3 = 3.0
    expect(screen.getByText('avg 3/cycle')).toBeInTheDocument();
  });
});

function makeResearchEntry(overrides: Partial<import('./DirectionsPanel').ResearchHistoryEntry> = {}): import('./DirectionsPanel').ResearchHistoryEntry {
  return {
    timestamp: '2026-01-01T00:00:00Z',
    task_id: 't-1',
    tasks_created: 1,
    finding_titles: [],
    key_finding_descriptions: [],
    ...overrides,
  };
}

describe('DirectionCard autonomous/assisted merge split', () => {
  it('falls back to legacy PRs shipped badge when attribution fields absent', () => {
    const d = makeDirection({
      research_history: [makeResearchEntry({ prs_merged: 5 })],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    expect(screen.getByText('5 PRs shipped')).toBeInTheDocument();
  });

  it('shows only auto badge when all merges are autonomous', () => {
    const d = makeDirection({
      research_history: [makeResearchEntry({ prs_merged: 8, autonomous_prs_merged: 8, assisted_prs_merged: 0 })],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    expect(screen.getByText('8 auto')).toBeInTheDocument();
    expect(screen.queryByText(/assisted/)).not.toBeInTheDocument();
  });

  it('shows only assisted badge when all merges are assisted', () => {
    const d = makeDirection({
      research_history: [makeResearchEntry({ prs_merged: 3, autonomous_prs_merged: 0, assisted_prs_merged: 3 })],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    expect(screen.getByText('3 assisted')).toBeInTheDocument();
    expect(screen.queryByText(/\bauto\b/)).not.toBeInTheDocument();
  });

  it('shows both auto and assisted badges when split', () => {
    const d = makeDirection({
      research_history: [makeResearchEntry({ prs_merged: 10, autonomous_prs_merged: 8, assisted_prs_merged: 2 })],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    expect(screen.getByText('8 auto')).toBeInTheDocument();
    expect(screen.getByText('2 assisted')).toBeInTheDocument();
  });

  it('shows 0 PRs shipped badge when attribution present but both zero', () => {
    const d = makeDirection({
      research_history: [makeResearchEntry({ prs_merged: 0, autonomous_prs_merged: 0, assisted_prs_merged: 0 })],
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    expect(screen.getByText('0 PRs shipped')).toBeInTheDocument();
  });

  it('shows self-sufficient badge when >=10 autonomous and zero operator actions', () => {
    const d = makeDirection({
      research_history: [makeResearchEntry({ prs_merged: 12, autonomous_prs_merged: 12, assisted_prs_merged: 0 })],
      history_aggregate: { total_operator_actions: 0 },
    });
    render(<DirectionCard {...makeProps(d)} />);
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    expect(screen.getByText('✓ self-sufficient')).toBeInTheDocument();
  });

  it('does not show self-sufficient badge when operator_action_count > 0', () => {
    const d = makeDirection({
      research_history: [makeResearchEntry({ prs_merged: 12, autonomous_prs_merged: 12, assisted_prs_merged: 0 })],
      history_aggregate: { total_operator_actions: 1 },
    });
    render(<DirectionCard {...makeProps(d)} />);
    // Self-sufficient badge not shown even when history is expanded (operator actions present)
    fireEvent.click(screen.getByRole('button', { name: /Research History/ }));
    expect(screen.queryByText('✓ self-sufficient')).not.toBeInTheDocument();
  });
});

describe('DirectionCard generalized outcome metrics', () => {
  it('shows generalized eval badge and per-kind breakdown chips when non-code lanes exist', () => {
    const d = makeDirection({
      generalized_successes: 6,
      generalized_health_rate: 0.75,
      outcome_kind_breakdown: {
        code_merge: { success: 3, neutral: 0, failure: 1 },
        research_quality: { success: 2, neutral: 0, failure: 0 },
        audit_findings: { success: 1, neutral: 1, failure: 0 },
      },
    });
    render(<DirectionCard {...makeProps(d)} />);

    expect(screen.getByText('6 eval · 75%')).toBeInTheDocument();
    expect(screen.getByText('code 3/4')).toBeInTheDocument();
    expect(screen.getByText('research 2/2')).toBeInTheDocument();
    expect(screen.getByText('audit 1/2')).toBeInTheDocument();
  });

  it('omits generalized UI for legacy directions with only code_merge outcomes', () => {
    const d = makeDirection({
      generalized_successes: 3,
      generalized_health_rate: 1,
      outcome_kind_breakdown: { code_merge: { success: 3, neutral: 0, failure: 0 } },
    });
    render(<DirectionCard {...makeProps(d)} />);

    expect(screen.queryByText(/\beval\b/)).not.toBeInTheDocument();
    expect(screen.queryByText('code 3/3')).not.toBeInTheDocument();
  });

  it('omits generalized UI when no breakdown is present', () => {
    render(<DirectionCard {...makeProps(makeDirection())} />);
    expect(screen.queryByText(/eval/)).not.toBeInTheDocument();
  });
});

describe('DirectionCard knowledge docs badge', () => {
  it('renders a clickable button when knowledge_document_count > 0 and onViewKnowledge is provided', () => {
    const onViewKnowledge = vi.fn();
    const d = makeDirection({ knowledge_document_count: 5 });
    const props = { ...makeProps(d), onViewKnowledge };
    render(<DirectionCard {...props} />);
    const btn = screen.getByRole('button', { name: '5 docs' });
    expect(btn).toBeInTheDocument();
    fireEvent.click(btn);
    expect(onViewKnowledge).toHaveBeenCalledWith('d-1');
  });

  it('renders a non-interactive span when onViewKnowledge is absent', () => {
    const d = makeDirection({ knowledge_document_count: 3 });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('3 docs')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '3 docs' })).not.toBeInTheDocument();
  });

  it('does not render the badge when knowledge_document_count is 0', () => {
    const d = makeDirection({ knowledge_document_count: 0 });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText(/docs/)).not.toBeInTheDocument();
  });

  it('renders "1 doc" (singular) for a single document', () => {
    const onViewKnowledge = vi.fn();
    const d = makeDirection({ knowledge_document_count: 1 });
    render(<DirectionCard {...{ ...makeProps(d), onViewKnowledge }} />);
    expect(screen.getByRole('button', { name: '1 doc' })).toBeInTheDocument();
  });
});

describe('DirectionCard cost budget editor', () => {
  it('renders the cost budget input in edit mode when setEditCostBudget is provided', () => {
    const d = makeDirection({ cost_budget_usd: 10.0 });
    const setEditCostBudget = vi.fn();
    const props = {
      ...makeProps(d),
      editingId: d.id,
      editText: d.text,
      editCostBudget: '10',
      setEditCostBudget,
    };
    render(<DirectionCard {...props} />);
    const input = screen.getByPlaceholderText('Budget $') as HTMLInputElement;
    expect(input).toBeInTheDocument();
    expect(input.value).toBe('10');
    fireEvent.change(input, { target: { value: '25.50' } });
    expect(setEditCostBudget).toHaveBeenCalledWith('25.50');
  });

  it('does not render the cost budget input when setEditCostBudget is absent', () => {
    const d = makeDirection();
    const props = { ...makeProps(d), editingId: d.id, editText: d.text };
    render(<DirectionCard {...props} />);
    expect(screen.queryByPlaceholderText('Budget $')).not.toBeInTheDocument();
  });
});

describe('DirectionCard staged retarget control', () => {
  function stagedProps(overrides: Partial<Direction> = {}) {
    const d = makeDirection({ status: 'staged', staged_for_version: 5, ...overrides });
    return {
      ...makeProps(d, 'staged'),
      activateStagedDirection: vi.fn(),
      cancelStagedDirection: vi.fn(),
      retargetStagedDirection: vi.fn(),
      stageTargetVersion: 5,
    };
  }

  it('retargets a staged direction to the entered version', () => {
    const props = stagedProps();
    render(<DirectionCard {...props} />);

    fireEvent.change(screen.getByPlaceholderText('5'), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: 'Retarget' }));

    expect(props.retargetStagedDirection).toHaveBeenCalledWith('d-1', 7);
  });

  it('disables the retarget button until a version is entered', () => {
    const props = stagedProps();
    render(<DirectionCard {...props} />);

    expect(screen.getByRole('button', { name: 'Retarget' })).toBeDisabled();
  });
});

describe('DirectionCard churn-retirement recommendation', () => {
  function churnRetireProps(overrides: Partial<Direction> = {}) {
    const d = makeDirection({
      status: 'paused',
      churn_retirement_recommended: true,
      churn_retirement_recommended_reason: 'Direction has exceeded max pause cycles with no merges.',
      churn_retirement_recommended_at: '2026-04-24T10:00:00Z',
      ...overrides,
    });
    return {
      ...makeProps(d, 'paused'),
      decideChurnRetirement: vi.fn(),
    };
  }

  it('renders retirement recommendation banner with reason and timestamp', () => {
    const props = churnRetireProps();
    render(<DirectionCard {...props} />);
    expect(screen.getByText('Retirement recommended')).toBeInTheDocument();
    expect(screen.getByText('Direction has exceeded max pause cycles with no merges.')).toBeInTheDocument();
  });

  it('renders Keep and Retire buttons when churn_retirement_recommended is true', () => {
    const props = churnRetireProps();
    render(<DirectionCard {...props} />);
    expect(screen.getByRole('button', { name: /Keep/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Retire/i })).toBeInTheDocument();
  });

  it('calls decideChurnRetirement with KEEP when Keep is clicked', () => {
    const props = churnRetireProps();
    render(<DirectionCard {...props} />);
    fireEvent.click(screen.getByRole('button', { name: /Keep/i }));
    expect(props.decideChurnRetirement).toHaveBeenCalledWith('d-1', 'KEEP');
  });

  it('shows inline retire confirm popover when Retire is clicked', () => {
    const props = churnRetireProps();
    render(<DirectionCard {...props} />);
    // Click the first Retire button to open the confirm popover
    const retireButtons = screen.getAllByRole('button', { name: /Retire/i });
    fireEvent.click(retireButtons[0]);
    expect(screen.getByText('Retire this direction?')).toBeInTheDocument();
  });

  it('calls decideChurnRetirement with RETIRE when confirm Retire is clicked', () => {
    const props = churnRetireProps();
    render(<DirectionCard {...props} />);
    // Open popover
    const retireButtons = screen.getAllByRole('button', { name: /Retire/i });
    fireEvent.click(retireButtons[0]);
    // Confirm
    const confirmButtons = screen.getAllByRole('button', { name: /Retire/i });
    fireEvent.click(confirmButtons[confirmButtons.length - 1]);
    expect(props.decideChurnRetirement).toHaveBeenCalledWith('d-1', 'RETIRE');
  });

  it('does not render banner when churn_retirement_recommended is false', () => {
    const props = churnRetireProps({ churn_retirement_recommended: false });
    render(<DirectionCard {...props} />);
    expect(screen.queryByText('Retirement recommended')).not.toBeInTheDocument();
  });

  it('does not render banner when churn_retirement_recommended is undefined', () => {
    const d = makeDirection({ status: 'paused' });
    render(<DirectionCard {...makeProps(d, 'paused')} />);
    expect(screen.queryByText('Retirement recommended')).not.toBeInTheDocument();
  });
});
