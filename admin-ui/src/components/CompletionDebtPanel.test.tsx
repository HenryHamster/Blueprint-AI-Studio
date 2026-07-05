import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const { fetchCompletionDebtsMock } = vi.hoisted(() => ({
  fetchCompletionDebtsMock: vi.fn(),
}));

vi.mock('../api', () => ({
  fetchCompletionDebts: (...args: unknown[]) => fetchCompletionDebtsMock(...args),
  dismissCompletionDebt: vi.fn(),
  resolveCompletionDebt: vi.fn(),
}));

import { CompletionDebtPanel } from './CompletionDebtPanel';

describe('CompletionDebtPanel', () => {
  beforeEach(() => {
    fetchCompletionDebtsMock.mockReset();
  });

  it('uses total_count for the open meta and truncation note', async () => {
    fetchCompletionDebtsMock.mockResolvedValueOnce({
      entries: [
        {
          id: 'debt-1',
          project_id: 'proj-1',
          kind: 'task',
          title: 'Resolve task drift',
          detail: 'Follow-up work is still open',
          status: 'open',
          dismiss_reason: null,
          created_at: '2026-07-04T00:00:00Z',
          dismissed_at: null,
          resolved_at: null,
        },
      ],
      count: 1,
      total_count: 2,
    });

    render(<CompletionDebtPanel projectId="proj-1" addToast={vi.fn()} />);

    expect(await screen.findByText('2 open')).toBeInTheDocument();
    expect(screen.queryByText('1 open')).not.toBeInTheDocument();
    expect(screen.getByText('Showing 1 of 2')).toBeInTheDocument();
    expect(screen.getByText('Resolve task drift')).toBeInTheDocument();
  });
});
