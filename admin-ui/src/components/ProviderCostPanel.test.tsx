import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { ProviderCostPanel } from './ProviderCostPanel';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
}));

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

describe('ProviderCostPanel', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing before data arrives and when rows are empty', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ window_hours: 24, rows: [] }));

    const { container } = render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    expect(container.firstChild).toBeNull();

    await act(async () => {});
    expect(container.firstChild).toBeNull();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/provider-cost-summary?hours=24', undefined);
  });

  it('renders table rows and appends project_id query parameter', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        window_hours: 24,
        rows: [
          {
            provider: 'openai',
            account_id: 'acc-1',
            account_name: 'Primary',
            total_tasks: 12,
            success_count: 10,
            merged_count: 4,
            total_cost_usd: 21.5,
            success_rate: 0.83,
            cost_per_merged_pr: 5.375,
          },
        ],
      }),
    );

    render(<ProviderCostPanel selectedProjectId="proj-abc" windowHours={24} />);

    expect(await screen.findByText('Provider Cost & Success')).toBeInTheDocument();
    expect(screen.getByText('Primary')).toBeInTheDocument();
    expect(screen.getByText('openai')).toBeInTheDocument();
    expect(screen.getByText('83%')).toBeInTheDocument();
    expect(screen.getByText('$21.50')).toBeInTheDocument();
    expect(screen.getByText('$5.38')).toBeInTheDocument();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/provider-cost-summary?hours=24&project_id=proj-abc', undefined);
  });

  it('polls every 30 seconds', async () => {
    vi.useFakeTimers();
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        window_hours: 24,
        rows: [
          {
            provider: 'openai',
            account_id: null,
            account_name: 'Default',
            total_tasks: 1,
            success_count: 1,
            merged_count: 1,
            total_cost_usd: 1,
            success_rate: 1,
            cost_per_merged_pr: 1,
          },
        ],
      }),
    );

    render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    await act(async () => {});
    expect(apiFetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    await act(async () => {});

    expect(apiFetchMock).toHaveBeenCalledTimes(2);
  });

  it('re-fetches when windowHours prop changes', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ window_hours: 24, rows: [] }));

    const { rerender } = render(<ProviderCostPanel selectedProjectId={null} windowHours={24} />);
    await act(async () => {});

    rerender(<ProviderCostPanel selectedProjectId={null} windowHours={168} />);
    await act(async () => {});

    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/provider-cost-summary?hours=24', undefined);
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/provider-cost-summary?hours=168', undefined);
  });
});
