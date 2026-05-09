import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TaskCard } from './TaskCard';
import type { Task } from '../types/task';

vi.mock('../api', () => ({
  authGet: vi.fn(() => Promise.resolve({ json: () => Promise.resolve({ stats: {} }) })),
  API_BASE_URL: 'http://localhost',
}));

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1',
    title: 'Test Task',
    description: 'desc',
    status: 'COMPLETED',
    task_type: 'create',
    created_at: '2024-01-01T00:00:00Z',
    created_by: 'USER',
    requires_approval: false,
    ...overrides,
  };
}

const baseProps = {
  onApprove: vi.fn(),
  onRequestChanges: vi.fn(),
  onCancel: vi.fn(),
  onRetry: vi.fn(),
  onEditPrompt: vi.fn(),
  onDismiss: vi.fn(),
  onArchive: vi.fn(),
  onViewLogs: vi.fn(),
};

describe('TaskCard SR badge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders no SR badge when context._self_review_outcome is absent', () => {
    render(<TaskCard task={makeTask()} {...baseProps} />);
    expect(screen.queryByText(/^SR/)).toBeNull();
  });

  it('renders no SR badge for soft-fallback (verdict=unknown, empty check_results)', () => {
    const task = makeTask({
      context: { _self_review_outcome: { verdict: 'unknown', check_results: {} } },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/^SR/)).toBeNull();
  });

  it('renders no SR badge for non-terminal status', () => {
    const task = makeTask({
      status: 'IN_PROGRESS',
      context: { _self_review_outcome: { verdict: 'pass', check_results: { correctness: { passed: true } } } },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/^SR/)).toBeNull();
  });

  it('renders SR ✓ badge for clean pass', () => {
    const task = makeTask({
      context: { _self_review_outcome: { verdict: 'pass', fixes_applied: false, check_results: { correctness: { passed: true } } } },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR ✓')).toBeInTheDocument();
    expect(screen.getByTitle('Self-review passed with no issues caught')).toBeInTheDocument();
  });

  it('renders SR ✓ + badge when fixes_applied=true with no failed checks', () => {
    const task = makeTask({
      context: { _self_review_outcome: { verdict: 'pass', fixes_applied: true, check_results: { correctness: { passed: true } } } },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR ✓ +')).toBeInTheDocument();
  });

  it('renders SR ✓ +N badge when fixes_applied=true and agent caught N issues', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          fixes_applied: true,
          check_results: {
            correctness: { passed: false, issue: 'found issue' },
            style: { passed: true },
          },
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR ✓ +1')).toBeInTheDocument();
  });

  it('renders SR ✗ badge when verdict=fail', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'fail',
          check_results: { correctness: { passed: false, issue: 'bug found' } },
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR ✗')).toBeInTheDocument();
  });

  it('renders SR miss N badge when N human-flagged categories not caught by agent', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: { verdict: 'pass', check_results: { correctness: { passed: true } } },
        _first_attempt_rejection_categories: { security: {}, 'error-handling': {} },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR miss 2')).toBeInTheDocument();
  });

  it('badge click calls onOpenSelfReview prop', async () => {
    const user = userEvent.setup();
    const onOpenSelfReview = vi.fn();
    const task = makeTask({
      context: { _self_review_outcome: { verdict: 'pass', check_results: { correctness: { passed: true } } } },
    });
    render(<TaskCard task={task} {...baseProps} onOpenSelfReview={onOpenSelfReview} />);
    await user.click(screen.getByTitle('Self-review passed with no issues caught'));
    expect(onOpenSelfReview).toHaveBeenCalledOnce();
  });

  it('badge has tooltip text matching the SR ✗ case', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'fail',
          check_results: { correctness: { passed: false } },
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR ✗')).toHaveAttribute('title', 'Self-review verdict: fail (1 failed check)');
  });

  it('renders SR badge for EVALUATING status', () => {
    const task = makeTask({
      status: 'EVALUATING',
      context: { _self_review_outcome: { verdict: 'pass', check_results: { correctness: { passed: true } } } },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR ✓')).toBeInTheDocument();
  });

  it('renders SR skipped badge when skip_reason is present', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'unknown',
          check_results: {},
          skip_reason: 'json_missing',
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR skipped')).toBeInTheDocument();
    expect(screen.getByTitle('Self-review skipped — JSON missing')).toBeInTheDocument();
  });

  it('renders SR skipped badge with reviewer_session_failed reason', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'unknown',
          check_results: {},
          skip_reason: 'reviewer_session_failed',
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByTitle('Self-review skipped — Reviewer session failed')).toBeInTheDocument();
  });

  it('does not render SR skipped badge when skip_reason is diff_too_small (policy intent)', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'unknown',
          check_results: {},
          skip_reason: 'diff_too_small',
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText('SR skipped')).toBeNull();
  });

  it('does not render SR skipped badge for IN_PROGRESS status even with skip_reason', () => {
    const task = makeTask({
      status: 'IN_PROGRESS',
      context: {
        _self_review_outcome: {
          verdict: 'unknown',
          check_results: {},
          skip_reason: 'json_missing',
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText('SR skipped')).toBeNull();
  });
});

describe('TaskCard SrDisallowedBadge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders disallowed badge when _filtered_blind_spots_count > 0', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { correctness: { passed: true } },
          _filtered_blind_spots_count: 2,
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('2 disallowed')).toBeTruthy();
  });

  it('renders no disallowed badge when _filtered_blind_spots_count is 0', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { correctness: { passed: true } },
          _filtered_blind_spots_count: 0,
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/disallowed/)).toBeNull();
  });

  it('renders no disallowed badge when _filtered_blind_spots_count is absent', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { correctness: { passed: true } },
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/disallowed/)).toBeNull();
  });
});

describe('TaskCard SrDivergenceBadge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders no divergence badge when no human categories', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { correctness: { passed: true } },
        },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/SR missed|SR over-flagged/)).toBeNull();
  });

  it('renders "SR missed" badge when reviewer flagged category that SR passed', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { correctness: { passed: true } },
        },
        _first_attempt_rejection_categories: { security: {} },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR missed security')).toBeInTheDocument();
  });

  it('renders "SR over-flagged" badge when SR failed category that reviewer did not flag', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'fail',
          check_results: { 'error-handling': { passed: false } },
        },
        _first_attempt_rejection_categories: {},
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR over-flagged error-handling')).toBeInTheDocument();
  });

  it('prioritizes "missed" over "over-flagged" when both exist', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { 'error-handling': { passed: false } },
        },
        _first_attempt_rejection_categories: { security: {} },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR missed security')).toBeInTheDocument();
    expect(screen.queryByText(/over-flagged/)).toBeNull();
  });

  it('does not render divergence badge when SR and reviewer agree on all categories', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { security: { passed: false } },
        },
        _first_attempt_rejection_categories: { security: {} },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/SR missed|SR over-flagged/)).toBeNull();
  });

  it('does not render divergence badge for IN_PROGRESS status', () => {
    const task = makeTask({
      status: 'IN_PROGRESS',
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { correctness: { passed: true } },
        },
        _first_attempt_rejection_categories: { security: {} },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/SR missed|SR over-flagged/)).toBeNull();
  });

  it('renders divergence badge for EVALUATING status', () => {
    const task = makeTask({
      status: 'EVALUATING',
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { correctness: { passed: true } },
        },
        _first_attempt_rejection_categories: { security: {} },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('SR missed security')).toBeInTheDocument();
  });

  it('divergence badge click calls onOpenSelfReview', async () => {
    const user = userEvent.setup();
    const onOpenSelfReview = vi.fn();
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { correctness: { passed: true } },
        },
        _first_attempt_rejection_categories: { security: {} },
      },
    });
    render(<TaskCard task={task} {...baseProps} onOpenSelfReview={onOpenSelfReview} />);
    await user.click(screen.getByText('SR missed security'));
    expect(onOpenSelfReview).toHaveBeenCalledOnce();
  });

  it('shows tooltip with all divergences when multiple exist', () => {
    const task = makeTask({
      context: {
        _self_review_outcome: {
          verdict: 'pass',
          check_results: { 'error-handling': { passed: false } },
        },
        _first_attempt_rejection_categories: { security: {}, performance: {} },
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    const badge = screen.getByText('SR missed performance');
    expect(badge).toHaveAttribute('title', expect.stringContaining('all divergences'));
  });
});
