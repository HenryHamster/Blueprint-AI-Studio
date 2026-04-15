import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RecentErrorsWidget } from './RecentErrorsWidget';

vi.mock('../api', () => ({
  apiFetch: vi.fn(),
  authHeaders: vi.fn().mockResolvedValue({ Authorization: 'Bearer ***' }),
  API_BASE_URL: '',
}));

import { apiFetch } from '../api';

const apiFetchMock = apiFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

describe('RecentErrorsWidget', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows the zero-error state when the buckets array is empty', async () => {
    apiFetchMock.mockResolvedValueOnce(jsonResponse({
      buckets: [],
      hours: 1,
      total_entries_scanned: 0,
      generated_at: new Date().toISOString(),
    }));

    render(<RecentErrorsWidget />);
    expect(await screen.findByText('No errors in last hour')).toBeInTheDocument();
  });

  it('shows a collapsed badge with the total count when errors exist', async () => {
    apiFetchMock.mockResolvedValueOnce(jsonResponse({
      buckets: [
        {
          exception_class: 'AttributeError',
          source: 'app/routers/agent.py:123',
          count: 12,
          first_seen_iso: new Date(Date.now() - 60_000).toISOString(),
          last_seen_iso: new Date(Date.now() - 30_000).toISOString(),
          sample_message: "AttributeError: 'Task' object has no attribute 'updated_at'",
        },
        {
          exception_class: 'LockNotAvailableError',
          source: 'app/rollups.py:456',
          count: 3,
          first_seen_iso: new Date(Date.now() - 120_000).toISOString(),
          last_seen_iso: new Date(Date.now() - 90_000).toISOString(),
          sample_message: 'LockNotAvailableError: could not obtain lock',
        },
      ],
      hours: 1,
      total_entries_scanned: 15,
      generated_at: new Date().toISOString(),
    }));

    render(<RecentErrorsWidget />);
    const toggle = await screen.findByRole('button', { name: /15 errors in last hour/i });
    expect(toggle).toBeInTheDocument();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('AttributeError')).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(toggle);

    await waitFor(() => {
      expect(toggle).toHaveAttribute('aria-expanded', 'true');
    });
    expect(screen.getByText('AttributeError')).toBeInTheDocument();
    expect(screen.getByText('LockNotAvailableError')).toBeInTheDocument();
  });

  it('shows a neutral error message when the API returns an error field', async () => {
    apiFetchMock.mockResolvedValueOnce(jsonResponse({
      buckets: [],
      hours: 1,
      error: 'PermissionDenied: logging.viewer required',
      generated_at: new Date().toISOString(),
    }));

    render(<RecentErrorsWidget />);
    expect(await screen.findByText('Error fetching log data')).toBeInTheDocument();
    expect(screen.queryByText('No errors in last hour')).not.toBeInTheDocument();
  });

  it('shows a neutral error message when the fetch fails at the network level', async () => {
    apiFetchMock.mockRejectedValueOnce(new Error('network down'));

    render(<RecentErrorsWidget />);
    expect(await screen.findByText('Error fetching log data')).toBeInTheDocument();
  });
});
