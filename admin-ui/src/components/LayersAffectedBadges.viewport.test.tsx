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
