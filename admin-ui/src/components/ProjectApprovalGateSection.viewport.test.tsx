import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import { ProjectApprovalGateSection } from './ProjectApprovalGateSection';

const customizedData = {
  approval_gate_config: {
    infra_files: ['backend/app/auth.py', '.github/workflows/deploy.yml'],
    critical_files: ['backend/app/security.py'],
  },
  is_default: false,
};

const emptyData = {
  approval_gate_config: { infra_files: [], critical_files: [] },
  is_default: true,
};

describe('ProjectApprovalGateSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { act(() => root.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  const mount = async (props: Parameters<typeof ProjectApprovalGateSection>[0]) => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<ProjectApprovalGateSection {...props} />);
    });
  };

  test('renders without horizontal overflow at 375px when customized', async () => {
    await expectNoOverflowAt(375, async () => {
      await mount({ data: customizedData, canSave: true, onSave: vi.fn(async () => {}) });
    });
  });

  test('renders without horizontal overflow at 375px when empty default', async () => {
    await expectNoOverflowAt(375, async () => {
      await mount({ data: emptyData, canSave: true, onSave: vi.fn(async () => {}) });
    });
  });

  test('path inputs and labels do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      await mount({ data: customizedData, canSave: true, onSave: vi.fn(async () => {}) });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      await mount({ data: customizedData, canSave: true, onSave: vi.fn(async () => {}) });
    }, { selector: 'button, a[href]' });
  });

  test('empty/onboarding collapses and its add-files button meets 44px at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      await mount({ data: emptyData, canSave: true, onSave: vi.fn(async () => {}) });
    }, { selector: 'button, a[href]' });
  });
});
