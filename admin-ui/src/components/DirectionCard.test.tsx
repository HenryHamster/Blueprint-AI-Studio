import { describe, it, expect, vi } from 'vitest';
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
