import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { FailureStageWidget } from './FailureStageWidget';

describe('FailureStageWidget', () => {
  it('renders nothing when distribution is null', () => {
    const { container } = render(<FailureStageWidget distribution={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when total_classified is zero', () => {
    const { container } = render(
      <FailureStageWidget distribution={{ stages: {}, total_classified: 0, dominant_stage: null }} />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders stage badges with counts', () => {
    render(
      <FailureStageWidget
        distribution={{
          stages: { test: 5, lint: 3, review: 2 },
          total_classified: 10,
          dominant_stage: 'test',
        }}
      />
    );
    expect(screen.getByText('Stage')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('marks dominant stage with title annotation', () => {
    render(
      <FailureStageWidget
        distribution={{
          stages: { lint: 7, build: 2 },
          total_classified: 9,
          dominant_stage: 'lint',
        }}
      />
    );
    const badge = screen.getByTitle('lint: 7 — dominant stage');
    expect(badge).toBeInTheDocument();
  });

  it('omits stages with zero count', () => {
    render(
      <FailureStageWidget
        distribution={{
          stages: { test: 0, lint: 4 },
          total_classified: 4,
          dominant_stage: 'lint',
        }}
      />
    );
    expect(screen.queryByTitle(/test:/)).toBeNull();
    expect(screen.getByTitle('lint: 4 — dominant stage')).toBeInTheDocument();
  });
});
