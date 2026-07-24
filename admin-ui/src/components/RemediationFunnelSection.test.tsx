import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RemediationFunnelSection } from './RemediationFunnelSection';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
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

const FUNNEL_WITH_SPAWNED = { investigations_spawned: 5 };
const RESIDUALS_DATA = {
  window_hours: 24,
  total_code_error_count: 10,
  residual_count: 2,
  top_residuals: [
    {
      fingerprint: 'fp-abc123',
      count: 3,
      most_recent_at: null,
      sample_message: 'TypeError: something went wrong',
      example_task_ids: ['task-uuid-1'],
    },
    {
      fingerprint: 'fp-def456',
      count: 2,
      most_recent_at: null,
      sample_message: 'AssertionError: unexpected value',
      example_task_ids: ['task-uuid-2'],
    },
  ],
};


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
    // The breakdown renders in both a mobile card layout and a desktop list, so
    // each class label appears twice — assert presence rather than uniqueness.
    expect(screen.getAllByText('CODE_ERROR').length).toBeGreaterThan(0);
    expect(screen.getAllByText('AGENT_TIMEOUT').length).toBeGreaterThan(0);
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

  describe('residual fingerprint ignore button', () => {
    function renderWithResiduals() {
      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('residual-fingerprints')) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(RESIDUALS_DATA) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      });
      render(<RemediationFunnelSection funnel={FUNNEL_WITH_SPAWNED} conversion={null} />);
    }

    async function openResiduals() {
      const toggle = await screen.findByRole('button', { name: /Unclassified CODE_ERROR/i });
      fireEvent.click(toggle);
    }

    it('shows an Ignore button for each fingerprint row', async () => {
      renderWithResiduals();
      await openResiduals();
      const ignoreButtons = screen.getAllByRole('button', { name: /Ignore fingerprint/i });
      expect(ignoreButtons).toHaveLength(2);
    });

    it('POSTs to the correct endpoint and optimistically removes the row on click', async () => {
      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('residual-fingerprints')) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(RESIDUALS_DATA) });
        }
        if (url.includes('residual-cases')) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      });
      render(<RemediationFunnelSection funnel={FUNNEL_WITH_SPAWNED} conversion={null} />);
      await openResiduals();

      const [firstIgnore] = screen.getAllByRole('button', { name: /Ignore fingerprint/i });
      fireEvent.click(firstIgnore);

      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/admin/residual-cases/fp-abc123/ignore',
        { method: 'POST' }
      );
      // Row replaced by undo affordance
      expect(screen.getByText('Ignored')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Undo/i })).toBeInTheDocument();
    });

    it('re-shows the row and displays an error when the POST fails', async () => {
      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('residual-fingerprints')) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(RESIDUALS_DATA) });
        }
        if (url.includes('residual-cases')) {
          return Promise.resolve({ ok: false, status: 500 });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      });
      render(<RemediationFunnelSection funnel={FUNNEL_WITH_SPAWNED} conversion={null} />);
      await openResiduals();

      const [firstIgnore] = screen.getAllByRole('button', { name: /Ignore fingerprint/i });
      fireEvent.click(firstIgnore);

      await waitFor(() => {
        expect(screen.getByText(/Failed to ignore/i)).toBeInTheDocument();
      });
      // Row is re-shown
      expect(screen.getByText('TypeError: something went wrong')).toBeInTheDocument();
    });

    it('clicking Undo restores the fingerprint row and removes the ignored placeholder', async () => {
      apiFetchMock.mockImplementation((url: string) => {
        if (url.includes('residual-fingerprints')) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve(RESIDUALS_DATA) });
        }
        if (url.includes('residual-cases')) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      });
      render(<RemediationFunnelSection funnel={FUNNEL_WITH_SPAWNED} conversion={null} />);
      await openResiduals();

      const [firstIgnore] = screen.getAllByRole('button', { name: /Ignore fingerprint/i });
      fireEvent.click(firstIgnore);

      // Optimistic state: row replaced by "Ignored" placeholder with Undo button
      expect(screen.getByText('Ignored')).toBeInTheDocument();
      const undoButton = screen.getByRole('button', { name: /Undo/i });

      fireEvent.click(undoButton);

      // Row should be restored and placeholder should be gone
      expect(screen.getByText('TypeError: something went wrong')).toBeInTheDocument();
      expect(screen.queryByText('Ignored')).toBeNull();
    });
  });

  describe('project-scoped residual fetch', () => {
    beforeEach(() => {
      apiFetchMock.mockReset();
      apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals()));
    });

    it('appends project_id to residual fingerprints URL when projectId prop is provided', async () => {
      render(<RemediationFunnelSection funnel={FUNNEL_WITH_SPAWNED} conversion={null} projectId="proj-abc" />);
      await act(async () => {});
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/admin/residual-fingerprints?hours=24&top_n=10&project_id=proj-abc',
        undefined,
      );
    });

    it('uses base URL without project_id when projectId prop is absent', async () => {
      render(<RemediationFunnelSection funnel={FUNNEL_WITH_SPAWNED} conversion={null} />);
      await act(async () => {});
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/admin/residual-fingerprints?hours=24&top_n=10',
        undefined,
      );
    });

    it('re-fetches with new project_id when projectId prop changes', async () => {
      const { rerender } = render(
        <RemediationFunnelSection funnel={FUNNEL_WITH_SPAWNED} conversion={null} projectId="proj-1" />,
      );
      await act(async () => {});

      rerender(<RemediationFunnelSection funnel={FUNNEL_WITH_SPAWNED} conversion={null} projectId="proj-2" />);
      await act(async () => {});

      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/admin/residual-fingerprints?hours=24&top_n=10&project_id=proj-1',
        undefined,
      );
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/admin/residual-fingerprints?hours=24&top_n=10&project_id=proj-2',
        undefined,
      );
    });
  });

  describe('show ignored toggle', () => {
    beforeEach(() => {
      apiFetchMock.mockReset();
    });

    it('renders a "Hide ignored" toggle (aria-pressed=false) when API returns IGNORED fingerprints — ignored items shown by default', async () => {
      apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
        baseCluster({ fingerprint: 'fp-ignored', case_status: 'IGNORED' }),
      ])));
      const user = userEvent.setup();
      render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
      await act(async () => {});
      await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
      const btn = screen.getByRole('button', { name: /Hide ignored/i });
      expect(btn).toBeInTheDocument();
      expect(btn).toHaveAttribute('aria-pressed', 'false');
    });

    it('fingerprints with case_status IGNORED are visible by default (showIgnored starts true)', async () => {
      apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
        baseCluster({ fingerprint: 'fp-ignored', case_status: 'IGNORED', sample_message: 'visible ignored msg' }),
      ])));
      const user = userEvent.setup();
      render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
      await act(async () => {});
      await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
      expect(screen.getByText('visible ignored msg')).toBeInTheDocument();
    });

    it('clicking "Hide ignored" hides IGNORED fingerprints and switches button to "Show ignored" (aria-pressed=true)', async () => {
      apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
        baseCluster({ fingerprint: 'fp-ignored', case_status: 'IGNORED', sample_message: 'hidden after toggle' }),
      ])));
      const user = userEvent.setup();
      render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
      await act(async () => {});
      await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
      await user.click(screen.getByRole('button', { name: /Hide ignored/i }));
      expect(screen.queryByText('hidden after toggle')).toBeNull();
      const btn = screen.getByRole('button', { name: /Show ignored/i });
      expect(btn).toHaveAttribute('aria-pressed', 'true');
    });

    it('clicking "Show ignored" again re-reveals IGNORED fingerprints and restores aria-pressed=false', async () => {
      apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
        baseCluster({ fingerprint: 'fp-ignored', case_status: 'IGNORED', sample_message: 'restored msg' }),
      ])));
      const user = userEvent.setup();
      render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
      await act(async () => {});
      await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
      await user.click(screen.getByRole('button', { name: /Hide ignored/i }));
      expect(screen.queryByText('restored msg')).toBeNull();
      await user.click(screen.getByRole('button', { name: /Show ignored/i }));
      expect(screen.getByText('restored msg')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Hide ignored/i })).toHaveAttribute('aria-pressed', 'false');
    });
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

describe('RemediationFunnelSection show/hide ignored toggle', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it('shows a Hide-ignored toggle when API returns IGNORED fingerprints', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ fingerprint: 'fp-ignored', case_status: 'IGNORED' }),
      baseCluster({ fingerprint: 'fp-active', case_status: 'ACTIVE' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.getByRole('button', { name: /Hide ignored/i })).toBeInTheDocument();
  });

  it('does not show the toggle when no fingerprints are API-IGNORED', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ fingerprint: 'fp-active', case_status: 'ACTIVE' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.queryByRole('button', { name: /Hide ignored|Show ignored/i })).toBeNull();
  });

  it('hides API-IGNORED fingerprints when toggle clicked, then re-shows on second click', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ fingerprint: 'fp-ignored', case_status: 'IGNORED', sample_message: 'the ignored error message' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));

    // Item visible initially (showIgnored defaults true)
    expect(screen.getByText('the ignored error message')).toBeInTheDocument();

    // Click hide
    await user.click(screen.getByRole('button', { name: /Hide ignored/i }));
    expect(screen.queryByText('the ignored error message')).toBeNull();

    // Click show again
    await user.click(screen.getByRole('button', { name: /Show ignored/i }));
    expect(screen.getByText('the ignored error message')).toBeInTheDocument();
  });

  it('does not show an Ignore button for IGNORED-status fingerprints', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ case_status: 'IGNORED' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.queryByRole('button', { name: /Ignore fingerprint/i })).toBeNull();
  });

  it('does not show an Ignore button for MERGED-status fingerprints', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ case_status: 'MERGED' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.queryByRole('button', { name: /Ignore fingerprint/i })).toBeNull();
  });
});

describe('RemediationFunnelSection unsuspend button', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it('shows an Unsuspend button for SUSPENDED fingerprints', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ case_status: 'SUSPENDED', consecutive_failure_count: 3 }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} projectId="proj-1" />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.getByRole('button', { name: /Unsuspend fingerprint/i })).toBeInTheDocument();
  });

  it('POSTs to the correct unsuspend endpoint with project_id on click', async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('residual-fingerprints')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(makeResiduals([
          baseCluster({ fingerprint: 'fp-suspended', case_status: 'SUSPENDED', consecutive_failure_count: 2 }),
        ])) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} projectId="proj-abc" />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));

    const unsuspendButton = screen.getByRole('button', { name: /Unsuspend fingerprint/i });
    await user.click(unsuspendButton);

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/residual-cases/fp-suspended/unsuspend?project_id=proj-abc',
      { method: 'POST' },
    );
  });

  it('does not show an Unsuspend button for ACTIVE fingerprints', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ case_status: 'ACTIVE' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.queryByRole('button', { name: /Unsuspend fingerprint/i })).toBeNull();
  });

  it('does not show an Unsuspend button for REMEDIATING fingerprints', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ case_status: 'REMEDIATING' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.queryByRole('button', { name: /Unsuspend fingerprint/i })).toBeNull();
  });

  it('does not show an Unsuspend button for IGNORED fingerprints', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ case_status: 'IGNORED' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.queryByRole('button', { name: /Unsuspend fingerprint/i })).toBeNull();
  });

  it('shows error and re-enables button when unsuspend POST fails', async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('residual-fingerprints')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(makeResiduals([
          baseCluster({ fingerprint: 'fp-fail-unsuspend', case_status: 'SUSPENDED', consecutive_failure_count: 1 }),
        ])) });
      }
      if (url.includes('unsuspend')) {
        return Promise.resolve({ ok: false, status: 500 });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));

    await user.click(screen.getByRole('button', { name: /Unsuspend fingerprint/i }));

    await waitFor(() => {
      expect(screen.getByText(/Failed to unsuspend/i)).toBeInTheDocument();
    });
    // Button should be re-shown after error
    expect(screen.getByRole('button', { name: /Unsuspend fingerprint/i })).toBeInTheDocument();
  });
});

describe('RemediationFunnelSection audit-deferred sub-metric', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it('shows audit-deferred sub-metric when cases have audit_defer_count > 0', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ fingerprint: 'fp-1', audit_defer_count: 2, audit_deferred_system_id: 'sys-a' }),
      baseCluster({ fingerprint: 'fp-2', audit_defer_count: 1, audit_deferred_system_id: 'sys-b' }),
    ])));
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    expect(screen.getByText(/2 cases audit-deferred/i)).toBeInTheDocument();
    expect(screen.getByText(/sys-a/)).toBeInTheDocument();
    expect(screen.getByText(/sys-b/)).toBeInTheDocument();
  });

  it('is hidden when no cases have audit_defer_count > 0', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ fingerprint: 'fp-1', audit_defer_count: 0 }),
    ])));
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    expect(screen.queryByText(/audit-deferred/i)).toBeNull();
  });

  it('shows only count (no system list) when unique systems exceed 3', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ fingerprint: 'fp-1', audit_defer_count: 1, audit_deferred_system_id: 'sys-a' }),
      baseCluster({ fingerprint: 'fp-2', audit_defer_count: 1, audit_deferred_system_id: 'sys-b' }),
      baseCluster({ fingerprint: 'fp-3', audit_defer_count: 1, audit_deferred_system_id: 'sys-c' }),
      baseCluster({ fingerprint: 'fp-4', audit_defer_count: 1, audit_deferred_system_id: 'sys-d' }),
    ])));
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} />);
    await act(async () => {});
    expect(screen.getByText(/4 cases audit-deferred/i)).toBeInTheDocument();
    expect(screen.queryByText(/on systems/i)).toBeNull();
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

  it('shows DEFERRED badge and subline when audit_defer_count > 0', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ audit_defer_count: 2, audit_deferred_system_id: 'sys-foo' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} projectId="p1" />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    expect(screen.getByText('DEFERRED ×2 · sys-foo')).toBeInTheDocument();
    expect(screen.getByText(/held by audit on sys-foo/)).toBeInTheDocument();
  });

  it('shows high-prominence DEFERRED badge when audit_defer_count >= 3', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(makeResiduals([
      baseCluster({ audit_defer_count: 4, audit_deferred_system_id: 'sys-bar' }),
    ])));
    const user = userEvent.setup();
    render(<RemediationFunnelSection funnel={{ investigations_spawned: 1 }} projectId="p1" />);
    await act(async () => {});
    await user.click(screen.getByRole('button', { name: /Unclassified CODE_ERROR patterns/i }));
    const badge = screen.getByText('DEFERRED ×4 · sys-bar');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain('bg-violet-500/30');
  });
});
