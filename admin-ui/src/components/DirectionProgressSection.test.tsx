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
