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
    setEditingId: vi.fn(),
    setEditText: vi.fn(),
    setEditPriority: vi.fn(),
    setEditStatus: vi.fn(),
    setEditCategory: vi.fn(),
    setEditPathTags: vi.fn(),
    setEditPathInput: vi.fn(),
    setEditCompletionCriteria: vi.fn(),
    startEdit: vi.fn(),
    updateDirection: vi.fn(),
    approveProposal: vi.fn(),
    rejectProposal: vi.fn(),
    reviveProposal: vi.fn(),
    reactivateDirection: vi.fn(),
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
    historyOpenIds: {},
    setHistoryOpenIds: vi.fn(),
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
    expect(screen.queryByText('High cost/PR')).not.toBeInTheDocument();
  });

  it('renders nothing when cost_per_merge_exceeds_threshold is absent', () => {
    const d = makeDirection();
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.queryByText('High cost/PR')).not.toBeInTheDocument();
  });

  it('renders amber warning badge when cost_per_merge_exceeds_threshold is true', () => {
    const d = makeDirection({ cost_per_merge_exceeds_threshold: true, cost_per_merged_pr: 45.00 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('High cost/PR');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toMatch(/amber/);
  });

  it('badge title includes the cost_per_merged_pr value', () => {
    const d = makeDirection({ cost_per_merge_exceeds_threshold: true, cost_per_merged_pr: 45.00 });
    render(<DirectionCard {...makeProps(d)} />);
    const badge = screen.getByText('High cost/PR');
    expect(badge.getAttribute('title')).toMatch(/\$45\.00/);
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
    expect(screen.getByText('High cost/PR')).toBeInTheDocument();
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
    expect(screen.getByText('High cost/PR')).toBeInTheDocument();
    // 30/40 = 75%, 10/40 = 25%.
    expect(screen.getByText(/claude-opus-4-7 75%/)).toBeInTheDocument();
    expect(screen.getByText(/gpt-5\.4 25%/)).toBeInTheDocument();
  });

  it('renders only the badge (no breakdown) when neither split source is provided', () => {
    const d = makeDirection({ cost_per_merge_exceeds_threshold: true, cost_per_merged_pr: 45.00 });
    render(<DirectionCard {...makeProps(d)} />);
    expect(screen.getByText('High cost/PR')).toBeInTheDocument();
    // No model labels should appear when there is no breakdown data.
    expect(screen.queryByText(/claude-opus-4-7/)).not.toBeInTheDocument();
    expect(screen.queryByText(/gpt-5\.4/)).not.toBeInTheDocument();
  });
});
