import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import { ProjectAutoReviewSection } from './ProjectAutoReviewSection';
import type { AutoReviewConfig } from '../types/project';

// Diverging from defaults so `hasChanges` flips true and the Save button is
// rendered enabled (canSave={true} alone is not enough).
const config: AutoReviewConfig = {
  enabled: true,
  max_per_hour: 12,
  max_concurrent: 6,
};

const renderSection = async (r: Root) => {
  await act(async () => {
    r.render(
      <ProjectAutoReviewSection
        config={config}
        canSave={true}
        onSave={vi.fn(async () => {})}
      />,
    );
  });
};

describe('ProjectAutoReviewSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px (admin, populated config)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderSection(r);
    });
  });

  test('rate-limit labels and validation messages do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderSection(r);
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderSection(r);
    }, { selector: 'button, a[href]' });
  });
});
