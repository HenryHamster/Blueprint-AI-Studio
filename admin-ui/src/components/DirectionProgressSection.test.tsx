import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

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
