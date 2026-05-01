import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DirectionAutoPauseWidget } from './DirectionAutoPauseWidget';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
}));

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

describe('DirectionAutoPauseWidget', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows a loading skeleton before the first response resolves', () => {
    apiFetchMock.mockImplementation(() => new Promise(() => {}));

    const { container } = render(<DirectionAutoPauseWidget selectedProjectId={null} />);

    expect(screen.getByText('Direction Auto-Pause')).toBeInTheDocument();
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument();
  });

  it('renders no-activity state and includes project_id in request when selected', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        auto_paused_count: 0,
        auto_recovered_count: 0,
        auto_retired_count: 0,
        currently_auto_paused: 0,
        top_reasons: [],
        trend: 'flat',
      }),
    );

    render(<DirectionAutoPauseWidget selectedProjectId="proj-123" />);

    expect(await screen.findByText('No auto-pause activity')).toBeInTheDocument();
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/direction-auto-pause-stats?window_hours=24&project_id=proj-123',
      undefined,
    );
  });

  it('re-fetches when window selection changes', async () => {
    const user = userEvent.setup();
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        auto_paused_count: 0,
        auto_recovered_count: 0,
        auto_retired_count: 0,
        currently_auto_paused: 0,
        top_reasons: [],
        trend: 'flat',
      }),
    );

    render(<DirectionAutoPauseWidget selectedProjectId={null} />);

    await screen.findByText('No auto-pause activity');
    await user.selectOptions(screen.getByRole('combobox'), '168');

    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/direction-auto-pause-stats?window_hours=168', undefined);
  });

  it('polls every 30 seconds', async () => {
    vi.useFakeTimers();
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        auto_paused_count: 0,
        auto_recovered_count: 0,
        auto_retired_count: 0,
        currently_auto_paused: 0,
        top_reasons: [],
        trend: 'flat',
      }),
    );

    render(<DirectionAutoPauseWidget selectedProjectId={null} />);
    await act(async () => {});
    expect(apiFetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    await act(async () => {});

    expect(apiFetchMock).toHaveBeenCalledTimes(2);
  });
});
