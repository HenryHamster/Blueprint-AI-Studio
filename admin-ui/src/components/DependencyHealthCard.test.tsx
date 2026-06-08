import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DependencyHealthCard } from './DependencyHealthCard';
import type { DependencyHealthSignals } from '../types/digest';

function makeData(overrides: Partial<DependencyHealthSignals> = {}): DependencyHealthSignals {
  return {
    parked_blockers: {
      total: 2,
      tasks: [
        {
          task_id: 't1',
          title: 'Parked alpha',
          project_id: 'p1',
          blocker_kind: 'Base re-cut conflict',
          blocked_reason: 'held',
          blocked_at: '2026-06-01T00:00:00',
          recheck_after: null,
        },
      ],
    },
    duplicate_foundation_candidates: {
      total: 1,
      candidates: [{ task_ids: ['a', 'b'], overlap_ratio: 0.5, shared_files: ['foo.py'] }],
    },
    unresumable_cancellations: {
      total: 0,
      by_class: {},
      by_direction: {},
      tasks: [],
    },
    ...overrides,
  };
}

describe('DependencyHealthCard', () => {
  it('renders the three counts', () => {
    render(<DependencyHealthCard data={makeData()} />);
    expect(screen.getByText('Parked blockers')).toBeInTheDocument();
    expect(screen.getByText('Duplicate foundations')).toBeInTheDocument();
    expect(screen.getByText('Unresumable cancels')).toBeInTheDocument();
  });

  it('drills down to the capped task list on click', () => {
    render(<DependencyHealthCard data={makeData()} />);
    expect(screen.queryByText('Parked alpha')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Parked blockers'));
    expect(screen.getByText('Parked alpha')).toBeInTheDocument();
    expect(screen.getByText(/Base re-cut conflict/)).toBeInTheDocument();
  });

  it('renders the affirmative healthy state when all failure counts are zero', () => {
    // No parked/duplicate/unresumable failures, but a live-blocked population
    // and resumes over the window — the card must render as a health gauge,
    // not disappear.
    render(
      <DependencyHealthCard
        data={makeData({
          parked_blockers: { total: 0, tasks: [] },
          duplicate_foundation_candidates: { total: 0, candidates: [] },
          unresumable_cancellations: { total: 0, tasks: [] },
          live_blocked: { total: 3, by_age: { '<1h': 2, '1-6h': 1 }, tasks: [] },
          resumes: { total: 5, by_resolved_by: { edge: 3, completed: 2 }, by_direction: {}, tasks: [] },
        })}
      />,
    );
    // Card renders (does not return null) — gauge title is present.
    expect(screen.getByText('Dependency Health')).toBeInTheDocument();
    // Affirmative line surfaces the live-blocked and resumed-this-window counts.
    const affirmative = screen.getByText(/blocked & waiting/);
    expect(affirmative).toHaveTextContent('3');
    expect(affirmative).toHaveTextContent('blocked & waiting');
    expect(affirmative).toHaveTextContent('5');
    expect(affirmative).toHaveTextContent('resumed this window');
  });

  it('renders nothing when data is null', () => {
    const { container } = render(<DependencyHealthCard data={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders structural give-up row with count and expands to show by_reason and findings', () => {
    render(
      <DependencyHealthCard
        data={makeData({
          finding_structural_give_up: {
            total: 3,
            by_reason: { out_of_scope: 2, impossible: 1 },
            by_direction: { dir1: 3 },
            findings: [{ task_id: 'task-g1', title: 'Task Give Up', reason: 'out_of_scope' }],
          },
        })}
      />,
    );
    expect(screen.getByText('Structural give-ups')).toBeInTheDocument();
    // Count not yet visible in expanded detail
    expect(screen.queryByText('out_of_scope')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Structural give-ups'));
    expect(screen.getByText('out_of_scope')).toBeInTheDocument();
    expect(screen.getByText('Task Give Up')).toBeInTheDocument();
  });

  it('give-ups contribute to anyFailure (checkmark absent)', () => {
    const { container } = render(
      <DependencyHealthCard
        data={makeData({
          parked_blockers: { total: 0, tasks: [] },
          duplicate_foundation_candidates: { total: 0, candidates: [] },
          unresumable_cancellations: { total: 0, tasks: [] },
          finding_structural_give_up: {
            total: 1,
            by_reason: { impossible: 1 },
            by_direction: {},
            findings: [],
          },
        })}
      />,
    );
    expect(container.textContent).not.toContain('✓');
  });

  it('renders stranded prerequisites row with count and expands to show split and demands', () => {
    render(
      <DependencyHealthCard
        data={makeData({
          missing_capability_demand: {
            total: 4,
            still_blocked: 3,
            already_cancelled: 1,
            by_direction: { dir1: 4 },
            demands: [
              {
                kind: 'foundation',
                ref: null,
                label: 'Auth Service',
                dependent_count: 3,
                still_blocked: 2,
                already_cancelled: 1,
                sample_task_ids: [],
                direction_ids: [],
                oldest_blocked_at: null,
                max_parked_reevals: 0,
              },
            ],
          },
        })}
      />,
    );
    expect(screen.getByText('Stranded prerequisites')).toBeInTheDocument();
    expect(screen.queryByText('still blocked')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Stranded prerequisites'));
    expect(screen.getByText('still blocked')).toBeInTheDocument();
    expect(screen.getByText('Auth Service')).toBeInTheDocument();
    expect(screen.getByText(/3 dependents/)).toBeInTheDocument();
  });

  it('missing_capability_demand contributes to anyFailure (checkmark absent)', () => {
    const { container } = render(
      <DependencyHealthCard
        data={makeData({
          parked_blockers: { total: 0, tasks: [] },
          duplicate_foundation_candidates: { total: 0, candidates: [] },
          unresumable_cancellations: { total: 0, tasks: [] },
          missing_capability_demand: {
            total: 2,
            still_blocked: 2,
            already_cancelled: 0,
            by_direction: {},
            demands: [],
          },
        })}
      />,
    );
    expect(container.textContent).not.toContain('✓');
  });

  it('demands fall back to ref then kind when label is absent', () => {
    render(
      <DependencyHealthCard
        data={makeData({
          missing_capability_demand: {
            total: 1,
            still_blocked: 1,
            already_cancelled: 0,
            by_direction: {},
            demands: [
              { kind: 'capability', ref: 'ref-abc', label: null, dependent_count: 1, still_blocked: 1, already_cancelled: 0, sample_task_ids: [], direction_ids: [], oldest_blocked_at: null, max_parked_reevals: 0 },
            ],
          },
        })}
      />,
    );
    fireEvent.click(screen.getByText('Stranded prerequisites'));
    expect(screen.getByText('ref-abc')).toBeInTheDocument();
  });
});
