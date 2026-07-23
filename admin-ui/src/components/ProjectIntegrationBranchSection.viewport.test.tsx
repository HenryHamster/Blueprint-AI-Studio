import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import { ProjectIntegrationBranchSection } from './ProjectIntegrationBranchSection';

const overrideData = {
  integration_branch: 'my-project/integration',
  is_default: false,
};

const defaultData = {
  integration_branch: 'main',
  is_default: true,
};

describe('ProjectIntegrationBranchSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px with per-project override', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectIntegrationBranchSection
            data={overrideData}
            canSave={true}
            onSave={vi.fn(async () => {})}
          />,
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px with global default', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectIntegrationBranchSection
            data={defaultData}
            canSave={true}
            onSave={vi.fn(async () => {})}
          />,
        );
      });
    });
  });

  test('branch input and labels do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectIntegrationBranchSection
            data={overrideData}
            canSave={true}
            onSave={vi.fn(async () => {})}
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
          <ProjectIntegrationBranchSection
            data={overrideData}
            canSave={true}
            onSave={vi.fn(async () => {})}
          />,
        );
      });
    }, { selector: 'button, a[href]' });
  });

  test('renders LTS context without overflow at 375px when promotion target differs', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectIntegrationBranchSection
            data={{ integration_branch: 'automain', is_default: false }}
            canSave={true}
            onSave={vi.fn(async () => {})}
            relatedPromotionTarget="main"
          />,
        );
      });
    });
  });
});
