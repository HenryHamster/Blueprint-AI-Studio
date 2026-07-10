import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { UILoadTimingPanel, percentile, fmtMs } from './UILoadTimingPanel';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(() => new Response('{}')),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({ apiFetch: apiFetchMock });
});

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

describe('percentile', () => {
  it('returns null for empty array', () => {
    expect(percentile([], 50)).toBeNull();
  });

  it('computes p50 for odd-length array', () => {
    expect(percentile([100, 200, 300], 50)).toBe(200);
  });

  it('computes p50 for even-length array', () => {
    expect(percentile([100, 200, 300, 400], 50)).toBe(200);
  });

  it('computes p90', () => {
    const vals = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000];
    expect(percentile(vals, 90)).toBe(900);
  });
});

describe('fmtMs', () => {
  it('formats null as em dash', () => {
    expect(fmtMs(null)).toBe('—');
  });

  it('formats values under 1000 as ms', () => {
    expect(fmtMs(823)).toBe('823ms');
  });

  it('formats 0 as 0ms', () => {
    expect(fmtMs(0)).toBe('0ms');
  });

  it('formats values >= 1000 as seconds', () => {
    expect(fmtMs(1842)).toBe('1.84s');
  });

  it('formats exactly 1000 as seconds', () => {
    expect(fmtMs(1000)).toBe('1.00s');
  });
});

describe('UILoadTimingPanel', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing before data arrives', () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ surfaces: {} }));
    const { container } = render(<UILoadTimingPanel />);
    expect(container.firstChild).toBeNull();
  });

  it('renders timing table on successful load', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        surfaces: {
          chat: [
            { ts: '2026-07-09T07:31:00Z', page_ready_ms: 823, sample_type: 'first_commit' },
            { ts: '2026-07-09T07:31:05Z', page_ready_ms: 345, sample_type: 'chat_data_ready' },
            { ts: '2026-07-09T08:00:00Z', page_ready_ms: 912, sample_type: 'first_commit' },
            { ts: '2026-07-09T08:00:05Z', page_ready_ms: 567, sample_type: 'chat_data_ready' },
          ],
        },
      }),
    );

    render(<UILoadTimingPanel />);
    await act(async () => {});

    expect(screen.getByText('UI Load Timing')).toBeInTheDocument();
    expect(screen.getByText('chat')).toBeInTheDocument();
    // p50 of [823, 912] = 823, distinct from p90 = 912
    expect(screen.getByText('823ms')).toBeInTheDocument();
    expect(screen.getByText('912ms')).toBeInTheDocument();
    expect(screen.getByText('345ms')).toBeInTheDocument();
  });

  it('shows error placeholder when fetch fails', async () => {
    apiFetchMock.mockRejectedValue(new Error('network error'));

    render(<UILoadTimingPanel />);
    await act(async () => {});

    expect(screen.getByText(/timing data unavailable/i)).toBeInTheDocument();
  });

  it('renders multiple sample type columns', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        surfaces: {
          chat: [
            { ts: '2026-07-09T07:31:00Z', page_ready_ms: 500, sample_type: 'first_commit' },
            { ts: '2026-07-09T07:31:05Z', page_ready_ms: 200, sample_type: 'chat_data_ready' },
          ],
          tasks: [
            { ts: '2026-07-09T07:32:00Z', page_ready_ms: 600, sample_type: 'first_commit' },
          ],
        },
      }),
    );

    render(<UILoadTimingPanel />);
    await act(async () => {});

    // Two surfaces rendered
    expect(screen.getByText('chat')).toBeInTheDocument();
    expect(screen.getByText('tasks')).toBeInTheDocument();
    // Sample type headers visible
    expect(screen.getByText('first_commit')).toBeInTheDocument();
    expect(screen.getByText('chat_data_ready')).toBeInTheDocument();
  });
});
