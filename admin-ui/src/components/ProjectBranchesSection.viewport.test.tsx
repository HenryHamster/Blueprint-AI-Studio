import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import { ProjectBranchesSection } from './ProjectBranchesSection';

const integrationBranchData = {
  integration_branch: 'automain',
  is_default: false,
};

const promotionTargetData = {
  promotion_target: 'main',
  is_default: false,
};

describe('ProjectBranchesSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders both sub-sections without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectBranchesSection
            integrationBranchData={integrationBranchData}
            onSaveIntegrationBranch={vi.fn(async () => {})}
            promotionTargetData={promotionTargetData}
            onSavePromotionTarget={vi.fn(async () => {})}
            canSave={true}
          />,
        );
      });
    });
  });

  test('branch names and labels do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectBranchesSection
            integrationBranchData={integrationBranchData}
            onSaveIntegrationBranch={vi.fn(async () => {})}
            promotionTargetData={promotionTargetData}
            onSavePromotionTarget={vi.fn(async () => {})}
            canSave={true}
          />,
        );
      });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectBranchesSection
            integrationBranchData={integrationBranchData}
            onSaveIntegrationBranch={vi.fn(async () => {})}
            promotionTargetData={promotionTargetData}
            onSavePromotionTarget={vi.fn(async () => {})}
            canSave={true}
          />,
        );
      });
    }, { selector: 'button, a[href]' });
  });

  test('renders read-only state for both sub-sections without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectBranchesSection
            integrationBranchData={integrationBranchData}
            onSaveIntegrationBranch={vi.fn(async () => {})}
            promotionTargetData={promotionTargetData}
            onSavePromotionTarget={vi.fn(async () => {})}
            canSave={false}
          />,
        );
      });
    });
  });
});
