import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../api', () => ({ apiFetch: vi.fn(), authHeaders: vi.fn(), API_BASE_URL: '' }));

import { apiFetch } from '../api';
import { RecentlyResolvedRegressionsPanel } from './RecentlyResolvedRegressionsPanel';

const apiFetchMock = apiFetch as unknown as ReturnType<typeof vi.fn>;
const response = (cases: unknown[]): Response => ({ ok: true, status: 200, json: async () => ({ cases }) } as Response);

beforeEach(() => apiFetchMock.mockReset());

describe('RecentlyResolvedRegressionsPanel', () => {
  it('shows time-to-detect when timestamps are present', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{ id: '1', regression_class: 'timeout', fix_pr_number: 42, recovered_at: '2026-05-06T10:15:00', first_seen_at: '2026-05-06T10:10:00', signal_first_crossed_at: '2026-05-06T10:05:00', time_to_recover_sec: 300 }]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('timeout');
    expect(screen.getByText('time-to-detect:').parentElement).toHaveTextContent('time-to-detect: 5m');
    expect(screen.getByText('time-to-recover:').parentElement).toHaveTextContent('time-to-recover: 5m');
  });

  it('shows unknown time-to-detect for legacy cases', async () => {
    apiFetchMock.mockResolvedValueOnce(response([{ id: '2', regression_class: 'timeout', fix_pr_number: null, recovered_at: null, first_seen_at: '2026-05-06T10:10:00', time_to_recover_sec: null }]));
    render(<RecentlyResolvedRegressionsPanel projectId="proj-1" />);
    await screen.findByText('timeout');
    expect(screen.getByText('time-to-detect:').parentElement).toHaveTextContent('time-to-detect: unknown');
  });
});
