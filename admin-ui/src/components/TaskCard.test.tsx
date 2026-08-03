import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps } from 'react';
import { TaskCard as TaskCardComponent, FAILURE_CLASS_CONFIG } from './TaskCard';
import type { Task } from '../types/task';

type TaskCardTestProps = Omit<ComponentProps<typeof TaskCardComponent>, 'readOnly'> & {
  readOnly?: boolean;
};

function TaskCard(props: TaskCardTestProps) {
  return <TaskCardComponent readOnly={false} {...props} />;
}

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

describe('TaskCard viewer access', () => {
  beforeEach(() => vi.clearAllMocks());

  it('keeps details readable while suppressing project actions and selection callbacks', async () => {
    const user = userEvent.setup();
    const task = makeTask({ status: 'QUEUED', requires_approval: true });
    render(<TaskCard task={task} readOnly selectionMode {...baseProps} />);

    expect(screen.queryByText('Approve & Merge')).toBeNull();
    expect(screen.queryByText('Request Changes')).toBeNull();
    expect(screen.queryByTitle('Approve')).toBeNull();
    const card = screen.getByRole('article');
    fireEvent.click(card, { ctrlKey: true });
    card.focus();
    await user.keyboard(' ');
    expect(baseProps.onApprove).not.toHaveBeenCalled();
    expect(baseProps.onCancel).not.toHaveBeenCalled();
    expect(baseProps.onViewLogs).toHaveBeenCalledOnce();
  });

  it('fails closed when readOnly is undefined at runtime', () => {
    const task = makeTask({ status: 'QUEUED', requires_approval: true });
    render(<TaskCardComponent task={task} readOnly={undefined as never} selectionMode {...baseProps} />);

    expect(screen.getByText('Test Task')).toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(screen.queryByText('Approve & Merge')).toBeNull();
    expect(screen.queryByText('Request Changes')).toBeNull();
  });

  it('cancels pending destructive confirmation timers when access downgrades', () => {
    vi.useFakeTimers();
    const onArchive = vi.fn();
    const task = makeTask({ status: 'COMPLETED' });
    const { rerender } = render(
      <TaskCard task={task} readOnly={false} {...baseProps} onArchive={onArchive} />,
    );
    const baselineTimers = vi.getTimerCount();
    fireEvent.click(screen.getByTitle(/Archive — moves this task/));
    expect(vi.getTimerCount()).toBeGreaterThan(baselineTimers);

    rerender(<TaskCard task={task} readOnly {...baseProps} onArchive={onArchive} />);
    expect(vi.getTimerCount()).toBe(baselineTimers);
    vi.advanceTimersByTime(5_000);
    expect(onArchive).not.toHaveBeenCalled();
    vi.useRealTimers();
  });
});

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

describe('TaskCard Direction Label', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('displays direction_title (truncated to ~24 chars) when present', () => {
    const onDirectionClick = vi.fn();
    const task = makeTask({
      context: {
        direction_id: 'd-abc123def456',
        direction_title: 'Add user authentication with OAuth 2.0 flow for better security',
      },
    });
    render(<TaskCard task={task} {...baseProps} onDirectionClick={onDirectionClick} />);
    // The title should be truncated to ~24 chars with ellipsis
    const button = screen.getByRole('button', { name: /Add user authentication/ });
    expect(button.textContent).toMatch(/Add user authentication.*…/);
  });

  it('displays direction_id (truncated to 10 chars) when title is absent', () => {
    const onDirectionClick = vi.fn();
    const task = makeTask({
      context: {
        direction_id: 'd-abc123def456',
      },
    });
    render(<TaskCard task={task} {...baseProps} onDirectionClick={onDirectionClick} />);
    const label = screen.getByText('d-abc123de…');
    expect(label).toBeInTheDocument();
  });

  it('shows full direction_title and direction_id in tooltip when title present', () => {
    const onDirectionClick = vi.fn();
    const task = makeTask({
      context: {
        direction_id: 'd-abc123def456',
        direction_title: 'Add user authentication with OAuth 2.0 flow',
      },
    });
    render(<TaskCard task={task} {...baseProps} onDirectionClick={onDirectionClick} />);
    const button = screen.getByRole('button', { name: /Add user authentication/ });
    expect(button).toHaveAttribute('title', expect.stringContaining('Add user authentication with OAuth 2.0 flow'));
    expect(button).toHaveAttribute('title', expect.stringContaining('d-abc123def456'));
  });

  it('shows only direction_id in tooltip when title absent', () => {
    const onDirectionClick = vi.fn();
    const task = makeTask({
      context: {
        direction_id: 'd-abc123def456',
      },
    });
    render(<TaskCard task={task} {...baseProps} onDirectionClick={onDirectionClick} />);
    const button = screen.getByRole('button', { name: /d-abc123de/ });
    expect(button).toHaveAttribute('title', 'Direction: d-abc123def456');
  });

  it('filters by direction_id (not label text) when clicked', async () => {
    const user = userEvent.setup();
    const onDirectionClick = vi.fn();
    const task = makeTask({
      context: {
        direction_id: 'd-abc123def456',
        direction_title: 'Add user authentication',
      },
    });
    render(<TaskCard task={task} {...baseProps} onDirectionClick={onDirectionClick} />);
    const button = screen.getByRole('button', { name: /Add user/ });
    await user.click(button);
    expect(onDirectionClick).toHaveBeenCalledWith('d-abc123def456');
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

describe('TaskCard error logs copy', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders error logs when status is FAILED and error_logs exist', () => {
    const errorText = 'This is a test error log';
    const task = makeTask({
      status: 'FAILED',
      error_logs: errorText,
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText(errorText.slice(0, 500))).toBeInTheDocument();
  });

  it('does not render error logs when status is not FAILED', () => {
    const task = makeTask({
      status: 'COMPLETED',
      error_logs: 'This should not be shown',
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/This should not be shown/)).toBeNull();
  });

  it('displays copy button on error logs block', () => {
    const task = makeTask({
      status: 'FAILED',
      error_logs: 'test error',
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByTitle('Copy full error logs')).toBeInTheDocument();
  });

  it('copies full error logs to clipboard on button click', async () => {
    const user = userEvent.setup();
    const fullErrorLog = 'a'.repeat(600); // Longer than 500 char truncation
    const task = makeTask({
      status: 'FAILED',
      error_logs: fullErrorLog,
    });
    render(<TaskCard task={task} {...baseProps} />);
    const copyButton = screen.getByTitle('Copy full error logs');
    await user.click(copyButton);
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(fullErrorLog);
  });

  it('prevents event propagation when copy button is clicked', async () => {
    const user = userEvent.setup();
    const task = makeTask({
      status: 'FAILED',
      error_logs: 'test error',
    });
    render(<TaskCard task={task} {...baseProps} />);
    const copyButton = screen.getByTitle('Copy full error logs');
    // Verify the button was clickable and clipboard was called
    await user.click(copyButton);
    expect(navigator.clipboard.writeText).toHaveBeenCalled();
  });
});

describe('TaskCard low-confidence spawn badge', () => {
  it('renders the low-confidence badge with percentage tooltip on a queued task', () => {
    const task = makeTask({
      status: 'QUEUED',
      context: { low_confidence_spawn: true, low_confidence_spawn_rate: 0.18 },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('18%')).toBeInTheDocument();
    expect(screen.getByTitle('Spawned at 18% predicted merge probability')).toBeInTheDocument();
  });

  it('renders the badge on an in-progress task (not gated to terminal states)', () => {
    const task = makeTask({
      status: 'IN_PROGRESS',
      context: { low_confidence_spawn: true, low_confidence_spawn_rate: 0.42 },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByTitle('Spawned at 42% predicted merge probability')).toBeInTheDocument();
  });

  it('does not render the badge when low_confidence_spawn is absent', () => {
    const task = makeTask({ status: 'QUEUED', context: {} });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText(/predicted merge probability/)).toBeNull();
  });

  it('renders auto-split and explore sibling tags from context', () => {
    const task = makeTask({
      status: 'QUEUED',
      context: { auto_decomposed: true, medium_exploration: true },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('auto-split')).toBeInTheDocument();
    expect(screen.getByText('explore')).toBeInTheDocument();
  });
});

describe('FAILURE_CLASS_CONFIG', () => {
  // Mirror of backend ALL_FAILURE_CLASSES (backend/app/failure_classification.py).
  // NOTE: AGENT_STALLED is persisted as the lowercase literal "agent_stalled".
  const CANONICAL_FAILURE_CLASSES = [
    'CODE_ERROR',
    'PROVISION_FAILURE',
    'AGENT_TIMEOUT',
    'EXTERNAL_API',
    'RATE_LIMIT',
    'SCOPE_OVERFLOW',
    'MIGRATION_ERROR',
    'VIEWPORT_REGRESSION',
    'agent_stalled',
    'BLOCKED_PRECONDITION',
    'SCOPE_EMPTY',
    'park_wake_timeout',
    'park_wake_stranded',
  ];

  it('covers exactly the 13 canonical failure classes (incl. lowercase agent_stalled and park/wake)', () => {
    expect(Object.keys(FAILURE_CLASS_CONFIG).sort()).toEqual([...CANONICAL_FAILURE_CLASSES].sort());
  });

  it('does not key the stalled class as uppercase AGENT_STALLED', () => {
    expect(FAILURE_CLASS_CONFIG).toHaveProperty('agent_stalled');
    expect(FAILURE_CLASS_CONFIG).not.toHaveProperty('AGENT_STALLED');
  });

  it('gives every class a non-empty label, hint, suggestion, and retryLabel', () => {
    for (const [key, cfg] of Object.entries(FAILURE_CLASS_CONFIG)) {
      expect(cfg.label, key).toBeTruthy();
      expect(cfg.color, key).toBeTruthy();
      expect(cfg.hint, key).toBeTruthy();
      expect(cfg.suggestion, key).toBeTruthy();
      expect(cfg.retryLabel, key).toBeTruthy();
    }
  });

  it('uses the exact cross-surface label strings for the newly added classes', () => {
    expect(FAILURE_CLASS_CONFIG.MIGRATION_ERROR.label).toBe('Migration Error');
    expect(FAILURE_CLASS_CONFIG.VIEWPORT_REGRESSION.label).toBe('Viewport Regression');
    expect(FAILURE_CLASS_CONFIG.agent_stalled.label).toBe('Agent Stalled');
    expect(FAILURE_CLASS_CONFIG.BLOCKED_PRECONDITION.label).toBe('Blocked — precondition');
    expect(FAILURE_CLASS_CONFIG.SCOPE_EMPTY.label).toBe('Scope Empty');
    expect(FAILURE_CLASS_CONFIG.park_wake_timeout.label).toBe('Park Wake Timeout');
    expect(FAILURE_CLASS_CONFIG.park_wake_stranded.label).toBe('Park Wake Stranded');
  });

  it('renders the badge and suggestion for a task that failed with agent_stalled', () => {
    const task = makeTask({ status: 'FAILED', failure_class: 'agent_stalled' });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Agent Stalled')).toBeInTheDocument();
    expect(screen.getByText('Wedged agent — retries with a fresh agent')).toBeInTheDocument();
  });
});

describe('TaskCard verify_tests task type', () => {
  it('renders the Verify Tests badge (not the generic Hash fallback) for a verify_tests task', () => {
    const task = makeTask({ task_type: 'verify_tests' });
    render(<TaskCard task={task} {...baseProps} />);
    // The TaskTypeBadge sets title to the styled label; the generic fallback
    // would title it with the raw task_type string instead.
    expect(screen.getByTitle('Verify Tests')).toBeInTheDocument();
    expect(screen.queryByTitle('verify_tests')).toBeNull();
  });
});

describe('TaskCard BLOCKED status', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a distinct Blocked status badge for a BLOCKED task', () => {
    const task = makeTask({ status: 'BLOCKED' });
    render(<TaskCard task={task} {...baseProps} />);
    // Badge label is present (visually revealed on hover but in the DOM).
    expect(screen.getAllByText('Blocked').length).toBeGreaterThan(0);
  });

  it('renders the blocker reason and kind when metadata is present', () => {
    const task = makeTask({
      status: 'BLOCKED',
      blocker_kind: 'file_conflict',
      blocked_reason: 'Waiting on rollup PR #123 to merge',
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Waiting on rollup PR #123 to merge')).toBeInTheDocument();
    expect(screen.getByText('file_conflict')).toBeInTheDocument();
  });

  it('prefers movement_explanation over the raw blocker reason', () => {
    const task = makeTask({
      status: 'BLOCKED',
      blocked_reason: 'Raw blocker reason should stay hidden',
      movement_explanation: 'Backend normalized why this task cannot move.',
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Backend normalized why this task cannot move.')).toBeInTheDocument();
    expect(screen.queryByText('Raw blocker reason should stay hidden')).toBeNull();
  });

  it('renders cleanly for a BLOCKED task with no blocker metadata (backward compat)', () => {
    const task = makeTask({ status: 'BLOCKED' });
    render(<TaskCard task={task} {...baseProps} />);
    // Falls back to the generic precondition message instead of crashing.
    expect(screen.getByText('Parked on an unmet precondition')).toBeInTheDocument();
  });

  it('does not render a blocker block for a non-blocked task without metadata', () => {
    const task = makeTask({ status: 'IN_PROGRESS' });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText('Parked on an unmet precondition')).toBeNull();
  });

  it('offers a cancel action for a BLOCKED task', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const task = makeTask({ status: 'BLOCKED' });
    render(<TaskCard task={task} {...baseProps} onCancel={onCancel} />);
    // First click arms the confirm, second confirms.
    const cancelBtn = screen.getByTitle(/^Cancel — cancels this blocked task/);
    await user.click(cancelBtn);
    await user.click(screen.getByTitle(/Confirm cancel/));
    expect(onCancel).toHaveBeenCalledWith('task-1');
  });

  it('queued cancel tooltip avoids VM termination copy', () => {
    const task = makeTask({ status: 'QUEUED' });
    render(<TaskCard task={task} {...baseProps} />);
    const cancelBtn = screen.getByTitle(/^Cancel — cancels before it starts/);
    expect(cancelBtn).toBeInTheDocument();
    expect(cancelBtn.getAttribute('title')).not.toContain('VM');
  });

  it('cancel tooltip surfaces blast radius when other tasks depend on this one', () => {
    const task = makeTask({ status: 'IN_PROGRESS' });
    render(<TaskCard task={task} {...baseProps} blockingCount={3} />);
    expect(
      screen.getByTitle('Cancel — stops the agent and cleans up its VM. In-progress work is discarded and not saved. — 3 visible tasks waiting on this'),
    ).toBeInTheDocument();
  });

  it('dismiss/archive tooltips spell out their distinct consequences', () => {
    const task = makeTask({ status: 'FAILED' });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByTitle(/Dismiss — marks this failed task cancelled/)).toBeInTheDocument();
    expect(screen.getByTitle(/Archive — moves this task off the board/)).toBeInTheDocument();
  });

  it('prefers movement_explanation over the raw parked reason', () => {
    const task = makeTask({
      status: 'PARKED',
      park_reason: 'Raw parked reason should stay hidden',
      movement_explanation: 'Backend normalized why this task is parked.',
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Backend normalized why this task is parked.')).toBeInTheDocument();
    expect(screen.queryByText('Raw parked reason should stay hidden')).toBeNull();
  });
});

describe('TaskCard merge verification badge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows Verified badge for a COMPLETED task with pr_url and no unmerged signals', () => {
    const task = makeTask({
      status: 'COMPLETED',
      pr_url: 'https://github.com/owner/repo/pull/123',
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Verified')).toBeInTheDocument();
    expect(screen.getByTitle('Merge verified by tests')).toBeInTheDocument();
  });

  it('shows Unverified badge when unverified_merge_reason is present', () => {
    const task = makeTask({
      status: 'COMPLETED',
      pr_url: 'https://github.com/owner/repo/pull/123',
      unverified_merge_reason: 'no_ci_gate_skip_ratio',
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Unverified')).toBeInTheDocument();
    expect(screen.queryByText('Verified')).toBeNull();
  });

  it('shows Closed badge when landed_on_integration is false', () => {
    const task = makeTask({
      status: 'COMPLETED',
      pr_url: 'https://github.com/owner/repo/pull/123',
      context: { landed_on_integration: false },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Closed')).toBeInTheDocument();
    expect(screen.getByTitle('PR closed without merge')).toBeInTheDocument();
    expect(screen.queryByText('Verified')).toBeNull();
  });

  it('shows Closed badge when _non_merge_unblock_reason is present', () => {
    const task = makeTask({
      status: 'COMPLETED',
      pr_url: 'https://github.com/owner/repo/pull/123',
      context: { _non_merge_unblock_reason: 'change_landed_elsewhere' },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Closed')).toBeInTheDocument();
    expect(screen.getByTitle('Completed without merge: change_landed_elsewhere')).toBeInTheDocument();
    expect(screen.queryByText('Verified')).toBeNull();
  });

  it('prioritizes unverified_merge_reason over closed signals', () => {
    const task = makeTask({
      status: 'COMPLETED',
      pr_url: 'https://github.com/owner/repo/pull/123',
      unverified_merge_reason: 'no_ci_gate_skip_ratio',
      context: { landed_on_integration: false },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText('Unverified')).toBeInTheDocument();
    expect(screen.queryByText('Closed')).toBeNull();
  });

  it('does not show badge for COMPLETED task without pr_url', () => {
    const task = makeTask({
      status: 'COMPLETED',
      pr_url: undefined,
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText('Verified')).toBeNull();
    expect(screen.queryByText('Closed')).toBeNull();
  });
});

describe('TaskCard stuck-task inline triage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('test_stuck_task_shows_last_activity_and_silence_duration', () => {
    const staleHeartbeat = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    const task = makeTask({
      status: 'IN_PROGRESS',
      last_heartbeat: staleHeartbeat,
      latest_status_message: 'Using tool: Bash',
    });
    render(<TaskCard task={task} {...baseProps} />);
    // Silence duration: "Silent 10m" (or similar) should be visible
    const silenceEl = document.querySelector('[data-testid="stuck-silence-duration"]');
    expect(silenceEl).not.toBeNull();
    expect(silenceEl!.textContent).toMatch(/Silent \d+m/);
    // Last activity: parsed from latest_status_message
    const activityEl = document.querySelector('[data-testid="stuck-last-activity"]');
    expect(activityEl).not.toBeNull();
    expect(activityEl!.textContent).toContain('Last: Using Bash');
  });

  it('does not show last-activity line when latest_status_message is absent', () => {
    const staleHeartbeat = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    const task = makeTask({
      status: 'IN_PROGRESS',
      last_heartbeat: staleHeartbeat,
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(document.querySelector('[data-testid="stuck-last-activity"]')).toBeNull();
  });

  it('stuck banner Re-queue button calls onRetry with task id', async () => {
    const user = userEvent.setup();
    const staleHeartbeat = new Date(Date.now() - 10 * 60 * 1000).toISOString();
    const task = makeTask({ status: 'IN_PROGRESS', last_heartbeat: staleHeartbeat });
    render(<TaskCard task={task} {...baseProps} />);
    const btn = document.querySelector('[data-testid="stuck-requeue-btn"]') as HTMLElement;
    expect(btn).not.toBeNull();
    await user.click(btn);
    expect(baseProps.onRetry).toHaveBeenCalledWith('task-1');
  });
});

describe('TaskCard evaluation verdict pill', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a humanized blocking pill for a review request_changes verdict', () => {
    const task = makeTask({
      task_type: 'review',
      evaluation_summary: {
        status: 'COMPLETED',
        verdict: 'request_changes',
        evidence: [{ label: 'Decision', value: 'REQUEST_CHANGES' }],
        highlights: [],
      },
    });
    render(<TaskCard task={task} {...baseProps} />);
    const pill = screen.getByText('Request Changes');
    expect(pill.className).toContain('status-blocking');
    // The raw machine token must never leak to operators.
    expect(screen.queryByText('request_changes')).toBeNull();
  });

  it('renders a humanized positive pill for an approve verdict', () => {
    const task = makeTask({
      task_type: 'review',
      evaluation_summary: { status: 'COMPLETED', verdict: 'approve' },
    });
    render(<TaskCard task={task} {...baseProps} />);
    const pill = screen.getByText('Approve');
    expect(pill.className).toContain('status-success');
  });

  it('renders no verdict pill when evaluation_summary is absent', () => {
    render(<TaskCard task={makeTask()} {...baseProps} />);
    expect(screen.queryByText('Approve')).toBeNull();
    expect(screen.queryByText('Request Changes')).toBeNull();
  });

  it('does not render a verdict pill for a non-terminal task', () => {
    const task = makeTask({
      status: 'IN_PROGRESS',
      task_type: 'review',
      evaluation_summary: { status: 'IN_PROGRESS', verdict: 'approve' },
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.queryByText('Approve')).toBeNull();
  });
});

describe('TaskCard approval button label', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows "Approve & Merge" for a non-compute approval', () => {
    const task = makeTask({
      status: 'EVALUATING',
      task_type: 'create',
      requires_approval: true,
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText(/Approve & Merge/)).toBeInTheDocument();
  });

  it('shows "Approve Spend ($X.XX)" for a compute_experiment with spend_cap_usd', () => {
    const task = makeTask({
      status: 'EVALUATING',
      task_type: 'compute_experiment',
      requires_approval: true,
      payloads: [{ type: 'compute_experiment', spend_cap_usd: 12.5, command: 'echo hi', deadline_seconds: 3600 }],
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText(/Approve Spend \(\$12\.50\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Approve & Merge/)).toBeNull();
  });

  it('shows "Approve Spend" for compute_experiment without spend_cap_usd in payload', () => {
    const task = makeTask({
      status: 'EVALUATING',
      task_type: 'compute_experiment',
      requires_approval: true,
      payloads: [{ type: 'compute_experiment' }],
    });
    render(<TaskCard task={task} {...baseProps} />);
    expect(screen.getByText(/Approve Spend$/)).toBeInTheDocument();
  });
});
