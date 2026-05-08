import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FailureStageWidget, type FailureStageDistribution } from './FailureStageWidget';
import { expectNoOverflowAt } from '../test/viewport';

const distribution: FailureStageDistribution = {
  stages: {
    provision: 4,
    clone: 2,
    test: 12,
    lint: 9,
    build: 5,
    pr_creation: 3,
    merge: 1,
    review: 7,
  },
  total_classified: 43,
  dominant_stage: 'test',
};

describe('FailureStageWidget — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FailureStageWidget distribution={distribution} />);
      });
    });
  });

  test('renders without horizontal overflow at 768px (tablet md: breakpoint)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FailureStageWidget distribution={distribution} />);
      });
    });
  });
});
