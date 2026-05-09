import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  LayersAffectedBadges,
  LayerMismatchBadge,
  UntargetedLayersBadge,
  type LayerSummary,
} from './LayersAffectedBadges';
import { expectNoOverflowAt } from '../test/viewport';

function makeSummary(layers: string[]): LayerSummary {
  return {
    cross_layer: layers.length >= 2,
    layers_affected: layers,
    window_hours: 48,
    layers: layers.map((layer) => ({
      layer,
      count: 3,
      distinct_fingerprints: 2,
      last_observed_at: new Date(Date.now() - 3600_000).toISOString(),
      sample: `sample message for ${layer} regression`,
    })),
  };
}

const sampleSummary: LayerSummary = {
  cross_layer: true,
  layers_affected: ['CI_WORKFLOW', 'AGENT_VM', 'AGENT_VERB'],
  window_hours: 24,
  layers: [
    {
      layer: 'CI_WORKFLOW',
      count: 12,
      distinct_fingerprints: 4,
      last_observed_at: '2026-05-08T00:00:00Z',
      sample: 'pytest exited 137',
    },
    {
      layer: 'AGENT_VM',
      count: 7,
      distinct_fingerprints: 3,
      last_observed_at: '2026-05-08T00:00:00Z',
      sample: 'libatk1.0-0 missing',
    },
    {
      layer: 'AGENT_VERB',
      count: 3,
      distinct_fingerprints: 2,
      last_observed_at: '2026-05-08T00:00:00Z',
      sample: 'verb timed out',
    },
  ],
  layer_mismatch: true,
  prior_fix_layers: ['CI_WORKFLOW'],
  current_symptom_layers: ['AGENT_VM', 'AGENT_VERB'],
};

describe('LayersAffectedBadges — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders all six canonical layers without overflow at 375px', async () => {
    const summary = makeSummary([
      'CI_WORKFLOW',
      'AGENT_VM',
      'AGENT_VERB',
      'BASELINE_GATE',
      'REVIEWER_PASS',
      'VIEWPORT',
    ]);
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<LayersAffectedBadges summary={summary} />);
      });
    });
  });

  test('renders VIEWPORT-only badge without overflow at 375px', async () => {
    const summary = makeSummary(['VIEWPORT']);
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<LayersAffectedBadges summary={summary} />);
      });
    });
  });

  test('renders null summary without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<LayersAffectedBadges summary={null} />);
      });
    });
  });

  test('LayersAffectedBadges renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<LayersAffectedBadges summary={sampleSummary} />);
      });
    });
  });

  test('LayerMismatchBadge renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <LayerMismatchBadge
            layer_mismatch={true}
            prior_fix_layers={['CI_WORKFLOW']}
            current_symptom_layers={['AGENT_VM', 'AGENT_VERB']}
          />,
        );
      });
    });
  });

  test('UntargetedLayersBadge renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <UntargetedLayersBadge untargeted_layers={['AGENT_VERB', 'BASELINE_GATE']} />,
        );
      });
    });
  });
});
