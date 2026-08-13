import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { acceptPendingConfirmation } from '../api';
import type { StrategicStateAttentionItem } from '../strategicStateApi';
import { expectMinTouchTargetAt, expectNoOverflowAt } from '../test/viewport';
import { PendingConfirmationsSection } from './PendingConfirmationsSection';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    acceptPendingConfirmation: vi.fn(async () => ({ status: 'accepted', type: 'credentials_needed' })),
    dismissPendingConfirmation: vi.fn(async () => {}),
  });
});

const ITEMS: StrategicStateAttentionItem[] = [
  {
    id: 'confirmation:credential',
    kind: 'confirmation',
    severity: 'needs_operator',
    reason: 'credentials_needed',
    label: 'Provide the deployment credential',
    detail: 'Provide the deployment credential before the next release can continue safely.',
    confirmation_type: 'credentials_needed',
    created_at: '2026-08-12T15:00:00Z',
    ref: { kind: 'confirmation', id: 'pending-credential', project_id: 'project-1' },
  },
  {
    id: 'confirmation:decision',
    kind: 'confirmation',
    severity: 'needs_operator',
    reason: 'major_decision',
    label: 'Choose the durable retention policy',
    detail: 'Choose whether durable conversation history should use the longer retention policy.',
    confirmation_type: 'major_decision',
    created_at: '2026-08-12T15:01:00Z',
    ref: { kind: 'confirmation', id: 'pending-decision', project_id: 'project-1' },
  },
];

describe('PendingConfirmationsSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const onResolved = vi.fn();

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.clearAllMocks();
  });

  function mount(canOperateProject = true) {
    if (root) {
      act(() => { root?.unmount(); });
      root = null;
    }
    host?.remove();
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root?.render(
        <PendingConfirmationsSection
          items={ITEMS}
          projectId="project-1"
          canOperateProject={canOperateProject}
          onResolved={onResolved}
        />,
      );
    });
  }

  test('actionable confirmation rows stay contained at 375px', async () => {
    await expectNoOverflowAt(375, mount);
    expect(host?.textContent).toContain('Pending confirmations');
    expect(host?.querySelector('[title="Mark provided"]')).toBeTruthy();
    expect(host?.querySelector('[title="Mark as resolved without action"]')).toBeTruthy();
  });

  test('accept and resolve controls remain touch-reachable at 375px', async () => {
    await expectMinTouchTargetAt(375, mount);
    const accept = host?.querySelector<HTMLButtonElement>('[title="Mark provided"]');
    expect(accept).toBeTruthy();

    await act(async () => { accept?.click(); });

    expect(acceptPendingConfirmation).toHaveBeenCalledWith(
      'project-1',
      'pending-credential',
      undefined,
    );
    expect(onResolved).toHaveBeenCalledOnce();
  });

  test('unknown operate permission stays contained and hides mutations at 375px', async () => {
    await expectNoOverflowAt(375, () => mount(false));
    expect(host?.textContent).toContain('Pending confirmations');
    expect(host?.querySelector('[title="Mark provided"]')).toBeNull();
    expect(host?.querySelector('[title="Mark as resolved without action"]')).toBeNull();
  });
});
