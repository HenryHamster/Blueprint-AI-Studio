import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RemediationFunnelSection } from './RemediationFunnelSection';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string) => apiFetchMock(path),
}));

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

function makeResiduals(top: unknown[] = []) {
  return { window_hours: 24, total_code_error_count: top.length, residual_count: top.length, top_residuals: top };
}

function baseCluster(overrides: Record<string, unknown> = {}) {
  return { fingerprint: 'fp-abc123', count: 2, most_recent_at: null, sample_message: 'test: unexpected', example_task_ids: [], ...overrides };
}

describe('RemediationFunnelSection', () => {
  beforeEach(() => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals()));
  });

  it('renders nothing when no investigations have been spawned', () => {
    const { container } = render(
      <RemediationFunnelSection
        funnel={{ investigations_spawned: 0 }}
        conversion={null}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when funnel is null', () => {
    const { container } = render(<RemediationFunnelSection funnel={null} conversion={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders stage counts and conversion percentages', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 10,
          investigations_completed: 8,
          investigations_with_diagnosis: 6,
          remediations_spawned: 4,
          remediations_merged: 2,
          remediations_failed: 1,
          active_investigations: 3,
          avg_time_to_remediation_hours: 1.5,
        }}
        conversion={null}
      />
    );
    expect(screen.getByText('Remediation Funnel')).toBeInTheDocument();
    expect(screen.getByText('Spawned')).toBeInTheDocument();
    expect(screen.getByText('10')).toBeInTheDocument();
    // 8/10 = 80%
    expect(screen.getByText(/80%/)).toBeInTheDocument();
    expect(screen.getByText('3 active')).toBeInTheDocument();
    expect(screen.getByText(/1.5h/)).toBeInTheDocument();
    expect(screen.getByText('1 failed')).toBeInTheDocument();
  });

  it('renders per-failure-class breakdown rows', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 5,
          investigations_completed: 4,
          investigations_spawned_by_class: { CODE_ERROR: 3, AGENT_TIMEOUT: 2 },
          investigations_completed_by_class: { CODE_ERROR: 2, AGENT_TIMEOUT: 2 },
          remediations_spawned_by_class: { CODE_ERROR: 2 },
          merged_remediations_by_trigger_class: { CODE_ERROR: 1 },
          failed_remediations_by_class: { AGENT_TIMEOUT: 1 },
        }}
        conversion={null}
      />
    );
    expect(screen.getByText('CODE_ERROR')).toBeInTheDocument();
    expect(screen.getByText('AGENT_TIMEOUT')).toBeInTheDocument();
    expect(screen.getByText('By failure class')).toBeInTheDocument();
  });

  it('renders drop-off reasons and structural filter pass rate', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 5,
          diagnosis_drop_off_by_reason: { structural: 2, low_confidence: 1 },
        }}
        conversion={{ parseable: 4, structural: 2 }}
      />
    );
    expect(screen.getByText('Diagnosis drop-off')).toBeInTheDocument();
    expect(screen.getByText('structural filter pass')).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByText('structural')).toBeInTheDocument();
    expect(screen.getByText('low conf')).toBeInTheDocument();
  });

  it('renders success-by-confidence rates', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 5,
          investigations_by_confidence: { high: 3, low: 1 },
          remediation_success_by_confidence: {
            high: { merged: 2, failed: 1 },
            low: { merged: 0, failed: 2 },
          },
        }}
        conversion={null}
      />
    );
    expect(screen.getByText('Confidence distribution')).toBeInTheDocument();
    expect(screen.getByText('Success by confidence')).toBeInTheDocument();
    expect(screen.getByText('2/3')).toBeInTheDocument();
    expect(screen.getByText('0/2')).toBeInTheDocument();
  });

  it('renders residual remediation funnel card with counts', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 5,
          residual_remediation_funnel: {
            spawned: 4,
            merged: 1,
            failed: 2,
            remediating: 2,
          },
        }}
        conversion={null}
      />
    );
    expect(screen.getByText('Residual remediation')).toBeInTheDocument();
    // spawned count
    expect(screen.getByText('4')).toBeInTheDocument();
    // remediating row
    expect(screen.getByText('remediating')).toBeInTheDocument();
    // at least two cells showing '2' (failed=2 and remediating=2)
    expect(screen.getAllByText('2').length).toBeGreaterThanOrEqual(2);
  });

  it('does not render residual funnel card when spawned is zero', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 5,
          residual_remediation_funnel: { spawned: 0, merged: 0, failed: 0, remediating: 0 },
        }}
        conversion={null}
      />
    );
    expect(screen.queryByText('Residual remediation')).toBeNull();
  });
});

describe('RemediationFunnelSection residuals status pill', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it('renders no status pill when case_status is absent', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([baseCluster()])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} projectId="p1" />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.queryByText(/REMEDIATING|SUSPENDED|IGNORED|MERGED|FAILED|ACTIVE/)).toBeNull();
  });

  it('shows REMEDIATING pill with PR number', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ case_status: 'REMEDIATING', last_spawned_pr_number: 42 }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} projectId="p1" />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.getByText('REMEDIATING #42')).toBeInTheDocument();
  });

  it('shows SUSPENDED pill with failure count', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ case_status: 'SUSPENDED', consecutive_failure_count: 3 }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} projectId="p1" />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.getByText('SUSPENDED ×3')).toBeInTheDocument();
  });

  it.each(['MERGED', 'FAILED', 'IGNORED', 'ACTIVE'] as const)(
    'shows %s status pill',
    async (status) => {
      apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
        baseCluster({ case_status: status }),
      ])));
      const user = userEvent.setup();
      render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} projectId="p1" />);
      await act(async () => {});
      await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
      expect(screen.getByText(status)).toBeInTheDocument();
    },
  );
});
