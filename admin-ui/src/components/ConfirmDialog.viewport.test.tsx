import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ConfirmDialog } from './ConfirmDialog';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

describe('ConfirmDialog — viewport regression', () => {
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

  test('renders with long title and message without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ConfirmDialog
            title="Are you sure you want to delete this research direction with all its associated findings and audit history?"
            message="This action cannot be undone. All research findings, cycle history, and strategic decisions tied to this direction will be permanently removed from the system. Any active tasks referencing this direction will be orphaned."
            confirmLabel="Delete Permanently"
            onConfirm={() => {}}
            onCancel={() => {}}
          />,
        );
      });
    });
  });

  test('renders with custom confirm label without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ConfirmDialog
            title="Disable Auto-Remediation for this Project"
            message="Disabling auto-remediation means that CI failures on your default branch will no longer automatically trigger fix PRs. You can re-enable this feature at any time from the project settings. Are you sure you want to proceed?"
            confirmLabel="Disable Feature"
            onConfirm={() => {}}
            onCancel={() => {}}
          />,
        );
      });
    });
  });

  test('renders with default confirm label without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ConfirmDialog
            title="This is a confirmation dialog with a very long title that might wrap to multiple lines on narrow viewports to ensure proper layout"
            message="A comprehensive message that explains the consequences of the action and provides context for the user's decision. This message should be long enough to test text wrapping and layout behavior at narrow widths."
            onConfirm={() => {}}
            onCancel={() => {}}
          />,
        );
      });
    });
  });
});
