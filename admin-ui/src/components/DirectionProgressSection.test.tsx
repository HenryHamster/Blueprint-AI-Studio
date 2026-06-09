import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import { DirectionProgressSection, type DirectionProgress } from './DirectionProgressSection';

function makeDirection(overrides: Partial<DirectionProgress> = {}): DirectionProgress {
  return {
    direction_id: 'dir-1',
    direction_text: 'Improve proof visibility',
    priority: 'high',
    cycles_in_window: 1,
    tasks_created: 2,
    prs_merged: 3,
    possibly_saturated: false,
    ...overrides,
  };
}

describe('DirectionProgressSection proof split', () => {
  it('renders proven merges and consumed downstream subset', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ proven_prs_merged: 2, consumed_prs_merged: 1 })]}
      />,
    );

    expect(screen.getByText('2 proven')).toBeInTheDocument();
    expect(screen.getByText('1 consumed')).toBeInTheDocument();
  });

  it('omits zero-only proof chips for legacy payloads', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ proven_prs_merged: 0, consumed_prs_merged: 0 })]}
      />,
    );

    expect(screen.queryByText('0 proven')).not.toBeInTheDocument();
    expect(screen.queryByText('0 consumed')).not.toBeInTheDocument();
  });
});

describe('DirectionProgressSection acceptance-evidence coverage', () => {
  it('renders verified_prs_merged chip when value is positive', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ verified_prs_merged: 4 })]}
      />,
    );

    expect(screen.getByText('4 verified')).toBeInTheDocument();
  });

  it('omits verified_prs_merged chip when value is zero', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ verified_prs_merged: 0 })]}
      />,
    );

    expect(screen.queryByText('0 verified')).not.toBeInTheDocument();
  });

  it('omits verified_prs_merged chip when field is absent', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection()]}
      />,
    );

    expect(screen.queryByText(/verified/)).not.toBeInTheDocument();
  });

  it('renders acceptance_declaration_coverage badge with percentage', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ acceptance_declaration_coverage: 0.72 })]}
      />,
    );

    expect(screen.getByText('72% decl')).toBeInTheDocument();
  });

  it('omits acceptance_declaration_coverage badge when field is null', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ acceptance_declaration_coverage: null })]}
      />,
    );

    expect(screen.queryByText(/% decl/)).not.toBeInTheDocument();
  });

  it('omits acceptance_declaration_coverage badge when field is absent', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection()]}
      />,
    );

    expect(screen.queryByText(/% decl/)).not.toBeInTheDocument();
  });
});

describe('DirectionProgressSection retirement eligible', () => {
  it('renders retirement eligible badge when flag is true', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ retirement_eligible: true })]}
      />,
    );

    expect(screen.getByText('retirement eligible')).toBeInTheDocument();
  });

  it('does not render retirement eligible badge when flag is false', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ retirement_eligible: false })]}
      />,
    );

    expect(screen.queryByText('retirement eligible')).not.toBeInTheDocument();
  });

  it('does not render retirement eligible badge when flag is undefined', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ retirement_eligible: undefined })]}
      />,
    );

    expect(screen.queryByText('retirement eligible')).not.toBeInTheDocument();
  });
});

describe('DirectionProgressSection spawn gate', () => {
  function expand() {
    const btn = screen.getByRole('button', { name: /Improve proof visibility/ });
    fireEvent.click(btn);
  }

  it('renders_research_spawn_gate_comparison — shows challenged/refuted/survived counts and merge rate comparison', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({
          research_spawn_gate: {
            challenged_count: 5,
            refuted_count: 2,
            survived_count: 3,
            challenged_spawned: 3,
            challenged_merged: 2,
            challenged_merge_rate: 0.67,
            unchallenged_spawned: 10,
            unchallenged_merged: 4,
            unchallenged_merge_rate: 0.40,
          },
        })]}
      />,
    );
    expand();

    const row = screen.getByText(/Spawn gate:/);
    expect(row).toBeInTheDocument();
    expect(row.textContent).toContain('5 challenged');
    expect(row.textContent).toContain('2 refuted');
    expect(row.textContent).toContain('3 survived');
    expect(row.textContent).toContain('67% challenged');
    expect(row.textContent).toContain('40% unchallenged');
  });

  it('omits spawn gate block when research_spawn_gate is null', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({ research_spawn_gate: null })]}
      />,
    );
    expand();

    expect(screen.queryByText(/Spawn gate:/)).not.toBeInTheDocument();
  });

  it('omits spawn gate block when research_spawn_gate field is absent (legacy payload)', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection()]}
      />,
    );
    expand();

    expect(screen.queryByText(/Spawn gate:/)).not.toBeInTheDocument();
  });

  it('renders em-dash for null merge rates when no spawned tasks exist yet', () => {
    render(
      <DirectionProgressSection
        directions={[makeDirection({
          research_spawn_gate: {
            challenged_count: 3,
            refuted_count: 1,
            survived_count: 2,
            challenged_spawned: 2,
            challenged_merged: 0,
            challenged_merge_rate: null,
            unchallenged_spawned: 5,
            unchallenged_merged: 0,
            unchallenged_merge_rate: null,
          },
        })]}
      />,
    );
    expand();

    const row = screen.getByText(/Spawn gate:/);
    expect(row.textContent).toContain('— challenged');
    expect(row.textContent).toContain('— unchallenged');
  });
});
