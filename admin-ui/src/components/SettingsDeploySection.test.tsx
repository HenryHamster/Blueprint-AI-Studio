import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { DeploySection } from './SettingsDeploySection';
import { PromotedAwaitingActivationCallout } from './VersionLifecyclePanelPrimitives';
import { apiFetch } from '../api';

vi.mock('../api', () => ({ apiFetch: vi.fn() }));

const noop = async () => {};

const baseProps = {
  deployState: null,
  onPausePipeline: noop,
  onResumePipeline: noop,
  onResetHealth: noop,
  onRollback: noop,
  onMarkStable: noop,
  onRepairState: noop,
  zoneHealth: null,
};

function settingsResp(threshold: number) {
  return new Response(JSON.stringify({ merge_grade_block_threshold: threshold }), { status: 200 });
}

function okResp() {
  return new Response(JSON.stringify({}), { status: 200 });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(apiFetch).mockImplementation(async (_url: string, opts?: RequestInit) => {
    if (opts?.method === 'PATCH') return okResp();
    return settingsResp(0.5);
  });
});

describe('MergeGradeGateSettings (via DeploySection)', () => {
  it('loads merge_grade_block_threshold from GET /api/admin/settings on mount and populates input', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      const input = screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement;
      expect(input.value).toBe('0.5');
    });
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith('/api/admin/settings');
  });

  it('renders the current threshold label from loaded value', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect(screen.getByText('0.5')).toBeInTheDocument();
    });
  });

  it('save button calls PATCH /api/admin/settings with the typed input value', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    fireEvent.change(screen.getByTestId('merge-grade-block-threshold-input'), { target: { value: '0.8' } });
    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    await waitFor(() => {
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        '/api/admin/settings',
        expect.objectContaining({
          method: 'PATCH',
          body: JSON.stringify({ merge_grade_block_threshold: 0.8 }),
        })
      );
    });
  });

  it('shows "Saved" after successful PATCH', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    await waitFor(() => {
      expect(screen.getByTestId('merge-grade-block-threshold-save').textContent).toBe('Saved');
    });
  });

  it('save button is disabled while save is in flight', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    let resolvePatch!: (v: Response) => void;
    const patchPromise = new Promise<Response>((res) => { resolvePatch = res; });
    vi.mocked(apiFetch).mockImplementationOnce(() => patchPromise);

    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    expect(screen.getByTestId('merge-grade-block-threshold-save')).toBeDisabled();
    expect(screen.getByTestId('merge-grade-block-threshold-save').textContent).toBe('Saving…');

    await act(async () => { resolvePatch(okResp()); });
  });

  it('shows validation error and does not call PATCH when input is NaN', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    fireEvent.change(screen.getByTestId('merge-grade-block-threshold-input'), { target: { value: 'abc' } });
    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    expect(screen.getByText('Must be a number between 0.0 and 1.0')).toBeInTheDocument();
    const patchCalls = vi.mocked(apiFetch).mock.calls.filter(([, opts]) => opts?.method === 'PATCH');
    expect(patchCalls).toHaveLength(0);
  });

  it('shows validation error when input is below 0', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    fireEvent.change(screen.getByTestId('merge-grade-block-threshold-input'), { target: { value: '-0.1' } });
    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    expect(screen.getByText('Must be a number between 0.0 and 1.0')).toBeInTheDocument();
  });

  it('shows validation error when input is above 1', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    fireEvent.change(screen.getByTestId('merge-grade-block-threshold-input'), { target: { value: '1.1' } });
    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    expect(screen.getByText('Must be a number between 0.0 and 1.0')).toBeInTheDocument();
  });

  it('shows API error detail when PATCH returns non-ok', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    vi.mocked(apiFetch).mockImplementationOnce(async () =>
      new Response(JSON.stringify({ detail: 'Server error occurred' }), { status: 500 })
    );

    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    await waitFor(() => {
      expect(screen.getByText('Server error occurred')).toBeInTheDocument();
    });
  });

  it('shows HTTP status when PATCH returns non-ok without detail', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    vi.mocked(apiFetch).mockImplementationOnce(async () =>
      new Response('not json', { status: 503 })
    );

    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    await waitFor(() => {
      expect(screen.getByText('HTTP 503')).toBeInTheDocument();
    });
  });

  it('accepts boundary value 0.0 without validation error', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    fireEvent.change(screen.getByTestId('merge-grade-block-threshold-input'), { target: { value: '0' } });
    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    await waitFor(() => {
      const patchCalls = vi.mocked(apiFetch).mock.calls.filter(([, opts]) => opts?.method === 'PATCH');
      expect(patchCalls).toHaveLength(1);
    });
    expect(screen.queryByText('Must be a number between 0.0 and 1.0')).toBeNull();
  });

  it('accepts boundary value 1.0 without validation error', async () => {
    render(<DeploySection {...baseProps} />);
    await waitFor(() => {
      expect((screen.getByTestId('merge-grade-block-threshold-input') as HTMLInputElement).value).toBe('0.5');
    });

    fireEvent.change(screen.getByTestId('merge-grade-block-threshold-input'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('merge-grade-block-threshold-save'));

    await waitFor(() => {
      const patchCalls = vi.mocked(apiFetch).mock.calls.filter(([, opts]) => opts?.method === 'PATCH');
      expect(patchCalls).toHaveLength(1);
    });
    expect(screen.queryByText('Must be a number between 0.0 and 1.0')).toBeNull();
  });
});

describe('activation deployment status', () => {
  it('explains that approval waits for the activation executor before a receipt can arrive', () => {
    render(
      <PromotedAwaitingActivationCallout
        version={{
          version_number: 4,
          status: 'promoted',
          opened_at: '2026-07-01T12:00:00Z',
          promoted_at: '2026-07-01T12:00:00Z',
          qualification_attempts: 1,
          last_qualification: null,
          serving: { status: 'unconfirmed' },
        }}
      />,
    );

    expect(screen.getByTestId('promoted-awaiting-activation')).toHaveTextContent('host-side activation executor');
    expect(screen.getByTestId('promoted-awaiting-activation')).toHaveTextContent(
      'must be installed and running before a serving receipt can arrive',
    );
    expect(screen.getByTestId('promoted-awaiting-activation')).not.toHaveTextContent('deploy agent');
  });
});
