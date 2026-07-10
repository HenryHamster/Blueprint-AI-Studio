import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { UILoadTimingPanel, percentile, fmtMs, computeStats, computePerTypeSplit } from './UILoadTimingPanel';

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

describe('computeStats', () => {
  it('returns zero count and null percentiles for empty input', () => {
    expect(computeStats([])).toEqual({ count: 0, p50: null, p90: null });
  });

  it('computes correct stats for a small array', () => {
    const result = computeStats([300, 100, 200]);
    expect(result.count).toBe(3);
    expect(result.p50).toBe(200);
    expect(result.p90).toBe(300);
  });

  it('does not mutate the input array', () => {
    const vals = [300, 100, 200];
    computeStats(vals);
    expect(vals).toEqual([300, 100, 200]);
  });
});

describe('computePerTypeSplit', () => {
  const samples = [
    { ts: '2026-07-09T20:00:00Z', page_ready_ms: 800, sample_type: 'first_commit' },
    { ts: '2026-07-09T21:00:00Z', page_ready_ms: 900, sample_type: 'first_commit' },
    { ts: '2026-07-09T23:00:00Z', page_ready_ms: 400, sample_type: 'first_commit' },
    { ts: '2026-07-09T20:00:00Z', page_ready_ms: 500, sample_type: 'chat_data_ready' },
    { ts: '2026-07-09T23:00:00Z', page_ready_ms: 250, sample_type: 'chat_data_ready' },
  ];
  const cutoff = '2026-07-09T22:09:00Z';

  it('partitions samples into before and after groups', () => {
    const result = computePerTypeSplit(samples, cutoff);
    expect(result['first_commit'].before.count).toBe(2);
    expect(result['first_commit'].after.count).toBe(1);
    expect(result['chat_data_ready'].before.count).toBe(1);
    expect(result['chat_data_ready'].after.count).toBe(1);
  });

  it('computes correct p50 for each group', () => {
    const result = computePerTypeSplit(samples, cutoff);
    // before first_commit: [800, 900] → p50 = 800
    expect(result['first_commit'].before.p50).toBe(800);
    // after first_commit: [400] → p50 = 400
    expect(result['first_commit'].after.p50).toBe(400);
  });

  it('returns count=0 and null percentiles when before group is empty', () => {
    const afterOnlySamples = [
      { ts: '2026-07-10T01:00:00Z', page_ready_ms: 300, sample_type: 'first_commit' },
    ];
    const result = computePerTypeSplit(afterOnlySamples, cutoff);
    expect(result['first_commit'].before.count).toBe(0);
    expect(result['first_commit'].before.p50).toBeNull();
    expect(result['first_commit'].before.p90).toBeNull();
    expect(result['first_commit'].after.count).toBe(1);
    expect(result['first_commit'].after.p50).toBe(300);
  });

  it('returns count=0 and null percentiles when after group is empty', () => {
    const beforeOnlySamples = [
      { ts: '2026-07-09T10:00:00Z', page_ready_ms: 700, sample_type: 'first_commit' },
    ];
    const result = computePerTypeSplit(beforeOnlySamples, cutoff);
    expect(result['first_commit'].after.count).toBe(0);
    expect(result['first_commit'].after.p50).toBeNull();
    expect(result['first_commit'].after.p90).toBeNull();
  });

  it('returns empty object for empty samples array', () => {
    expect(computePerTypeSplit([], cutoff)).toEqual({});
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
    // All samples are before the default cutoff (2026-07-09T22:09), so before group shows stats
    // before first_commit: [823, 912] → p50=823, p90=912
    expect(screen.getByText('823ms')).toBeInTheDocument();
    expect(screen.getByText('912ms')).toBeInTheDocument();
    // before chat_data_ready: [345, 567] → p50=345
    expect(screen.getByText('345ms')).toBeInTheDocument();
    // after group has no samples → shows "no post-cutoff data"
    expect(screen.getAllByText('no post-cutoff data').length).toBeGreaterThan(0);
  });

  it('shows "no baseline" message when all samples are after the cutoff', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        surfaces: {
          chat: [
            { ts: '2026-07-10T10:00:00Z', page_ready_ms: 200, sample_type: 'first_commit' },
          ],
        },
      }),
    );

    render(<UILoadTimingPanel />);
    await act(async () => {});

    expect(screen.getByText(/no baseline — overwritten by 200-sample cap/i)).toBeInTheDocument();
    // after group shows the value (p50 and p90 both 200ms for a single sample)
    expect(screen.getAllByText('200ms').length).toBeGreaterThan(0);
  });

  it('shows "no post-cutoff data" message when all samples are before the cutoff', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        surfaces: {
          chat: [
            { ts: '2026-07-08T10:00:00Z', page_ready_ms: 800, sample_type: 'first_commit' },
          ],
        },
      }),
    );

    render(<UILoadTimingPanel />);
    await act(async () => {});

    expect(screen.getByText(/no post-cutoff data/i)).toBeInTheDocument();
    // before group shows the value (p50 and p90 both 800ms for a single sample)
    expect(screen.getAllByText('800ms').length).toBeGreaterThan(0);
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

  it('shows before/after column sub-headers', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        surfaces: {
          chat: [
            { ts: '2026-07-09T07:31:00Z', page_ready_ms: 500, sample_type: 'first_commit' },
          ],
        },
      }),
    );

    render(<UILoadTimingPanel />);
    await act(async () => {});

    expect(screen.getByText('before p50')).toBeInTheDocument();
    expect(screen.getByText('after p50')).toBeInTheDocument();
  });
});
