import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CompletionDebtPanel } from './CompletionDebtPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    fetchCompletionDebts: vi.fn(async () => ({
      entries: [
        {
          id: 'debt-1',
          project_id: 'proj-1',
          kind: 'task',
          title: 'Resolve outstanding follow-up from task feat/add-mobile-responsive-grid',
          detail: 'The follow-up work identified during review is still open.',
          status: 'open',
          dismiss_reason: null,
          created_at: '2026-07-01T00:00:00Z',
          dismissed_at: null,
          resolved_at: null,
        },
        {
          id: 'debt-2',
          project_id: 'proj-1',
          kind: 'review',
          title: 'Address review comment about missing error handling in apiFetch wrapper',
          detail: null,
          status: 'open',
          dismiss_reason: null,
          created_at: '2026-07-03T00:00:00Z',
          dismissed_at: null,
          resolved_at: null,
        },
      ],
      count: 2,
      total_count: 2,
    })),
    dismissCompletionDebt: vi.fn(),
    resolveCompletionDebt: vi.fn(),
  };
});

describe('CompletionDebtPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<CompletionDebtPanel projectId="proj-1" addToast={vi.fn()} />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });

  test('renders without horizontal overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<CompletionDebtPanel projectId="proj-1" addToast={vi.fn()} />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });
});
