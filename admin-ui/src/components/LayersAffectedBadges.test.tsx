import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { LayersAffectedBadges, LayerMismatchBadge, type LayerSummary } from './LayersAffectedBadges';

const summary = (overrides: Partial<LayerSummary> = {}): LayerSummary => ({
  cross_layer: false,
  layers_affected: ['CI_WORKFLOW'],
  window_hours: 48,
  layers: [
    {
      layer: 'CI_WORKFLOW',
      count: 3,
      distinct_fingerprints: 2,
      last_observed_at: '2026-05-07T11:00:00',
      sample: 'pytest fixture missing',
    },
  ],
  ...overrides,
});

describe('LayersAffectedBadges', () => {
  it('renders nothing when summary is null/undefined', () => {
    const { container } = render(<LayersAffectedBadges summary={null} />);
    expect(container.firstChild).toBeNull();
    const undef = render(<LayersAffectedBadges summary={undefined} />);
    expect(undef.container.firstChild).toBeNull();
  });

  it('renders nothing when layers array is empty', () => {
    const { container } = render(
      <LayersAffectedBadges
        summary={{ cross_layer: false, layers_affected: [], window_hours: 48, layers: [] }}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders one badge per layer with short label and count', () => {
    render(<LayersAffectedBadges summary={summary()} />);
    const badge = screen.getByTestId('layer-badge-CI_WORKFLOW');
    expect(badge).toHaveTextContent('CI · 3');
    expect(badge.getAttribute('title')).toContain('sample: pytest fixture missing');
    expect(badge.getAttribute('title')).toContain('last seen 2026-05-07T11:00:00');
  });

  it('shows cross-layer chip only when cross_layer is true', () => {
    const { rerender } = render(<LayersAffectedBadges summary={summary()} />);
    expect(screen.queryByTestId('cross-layer-badge')).toBeNull();

    rerender(
      <LayersAffectedBadges
        summary={summary({
          cross_layer: true,
          layers_affected: ['AGENT_VM', 'CI_WORKFLOW'],
          layers: [
            {
              layer: 'CI_WORKFLOW',
              count: 3,
              distinct_fingerprints: 2,
              last_observed_at: '2026-05-07T11:00:00',
              sample: 'ci sample',
            },
            {
              layer: 'AGENT_VM',
              count: 1,
              distinct_fingerprints: 1,
              last_observed_at: '2026-05-07T10:00:00',
              sample: 'vm sample',
            },
          ],
        })}
      />,
    );
    expect(screen.getByTestId('cross-layer-badge')).toBeInTheDocument();
    expect(screen.getByTestId('layer-badge-CI_WORKFLOW')).toHaveTextContent('CI · 3');
    expect(screen.getByTestId('layer-badge-AGENT_VM')).toHaveTextContent('VM · 1');
  });

  it('falls back to full layer name when not in short-label table', () => {
    render(
      <LayersAffectedBadges
        summary={summary({
          layers_affected: ['UNKNOWN_LAYER'],
          layers: [
            {
              layer: 'UNKNOWN_LAYER',
              count: 1,
              distinct_fingerprints: 1,
              last_observed_at: '2026-05-07T11:00:00',
              sample: '',
            },
          ],
        })}
      />,
    );
    expect(screen.getByTestId('layer-badge-UNKNOWN_LAYER')).toHaveTextContent('UNKNOWN_LAYER · 1');
  });
});

describe('LayerMismatchBadge', () => {
  it('renders nothing when layer_mismatch is absent or false (no prior fixes)', () => {
    const { container } = render(<LayerMismatchBadge />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when prior and current layers overlap (no mismatch)', () => {
    const { container } = render(
      <LayerMismatchBadge
        layer_mismatch={false}
        prior_fix_layers={['CI_WORKFLOW']}
        current_symptom_layers={['CI_WORKFLOW']}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders badge with disjoint-layer tooltip when layer_mismatch is true', () => {
    render(
      <LayerMismatchBadge
        layer_mismatch={true}
        prior_fix_layers={['CI_WORKFLOW']}
        current_symptom_layers={['AGENT_VM']}
      />,
    );
    const badge = screen.getByTestId('layer-mismatch-badge');
    expect(badge).toHaveTextContent('layer mismatch');
    expect(badge.getAttribute('title')).toBe('prior fixes targeted CI; current symptoms on VM');
  });

  it('uses full layer name for unknown layers in tooltip', () => {
    render(
      <LayerMismatchBadge
        layer_mismatch={true}
        prior_fix_layers={['CUSTOM_LAYER']}
        current_symptom_layers={['AGENT_VM']}
      />,
    );
    const badge = screen.getByTestId('layer-mismatch-badge');
    expect(badge.getAttribute('title')).toBe('prior fixes targeted CUSTOM_LAYER; current symptoms on VM');
  });
});
