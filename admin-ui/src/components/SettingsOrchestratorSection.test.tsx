import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsOrchestratorSection } from './SettingsOrchestratorSection';
import {
  fetchMetricsIntegrityObservability,
  patchMetricsIntegritySettings,
  patchCapabilityProofGateSettings,
  apiFetch,
} from '../api';

vi.mock('../api', () => ({
  fetchEvergreenFillSettings: vi.fn().mockResolvedValue({
    evergreen_fill_mode: 'off',
    evergreen_fill_interval_minutes: 15,
    evergreen_fill_util_threshold: 0.5,
    evergreen_fill_min_free_slots: 2,
    evergreen_fill_max_active: 1,
    evergreen_fill_daily_cap: 3,
    evergreen_fill_deferred_memo_enrollment: false,
  }),
  patchEvergreenFillSettings: vi.fn().mockResolvedValue(undefined),
  patchStructuralHealthSettings: vi.fn().mockResolvedValue(undefined),
  patchMetricsIntegritySettings: vi.fn().mockResolvedValue(undefined),
  patchCapabilityProofGateSettings: vi.fn().mockResolvedValue(undefined),
  apiFetch: vi.fn().mockResolvedValue({
    ok: true,
    json: vi.fn().mockResolvedValue({
      structural_health_audit_mode: 'off',
      metrics_integrity_sweep_mode: 'off',
      capability_proof_gate_mode: 'off',
      capability_proof_gate_auto_graduation_enabled: false,
      capability_proof_gate_auto_graduation_observe_threshold: 0.5,
      capability_proof_gate_auto_graduation_enforce_threshold: 0.95,
    }),
  }),
  fetchEvergreenFillObservability: vi.fn().mockResolvedValue({
    created_count: 0,
    reason_counts: {},
    trigger_counts: {},
    recent: [],
  }),
  fetchEvergreenBacklog: vi.fn().mockResolvedValue({ items: [] }),
  fetchEvergreenBacklogSummary: vi.fn().mockResolvedValue({}),
  fetchMetricsIntegrityObservability: vi.fn().mockResolvedValue({
    ticks: 0,
    checks_run: 0,
    flags_total: 0,
    errors_total: 0,
    enrolled_total: 0,
    last_tick_at: null,
    last_tick: null,
    recent_ticks: [],
    recent_flags: [],
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

const baseProps = {
  maxConcurrentVMs: 4,
  onUpdateMaxVMs: vi.fn(),
  maxRetries: 3,
  onUpdateMaxRetries: vi.fn(),
  requireApprovalGates: false,
  onUpdateRequireApprovalGates: vi.fn(),
  autoArchiveEnabled: false,
  onUpdateAutoArchiveEnabled: vi.fn(),
  autoArchiveTerminalAfterDays: 7,
  onUpdateAutoArchiveTerminalAfterDays: vi.fn(),
  autoArchiveMaxTerminalTasksPerProject: 0,
  onUpdateAutoArchiveMaxTerminalTasksPerProject: vi.fn(),
  autoArchiveIntervalMinutes: 60,
  onUpdateAutoArchiveIntervalMinutes: vi.fn(),
  maxAutonomousTasksPerHour: 10,
  onUpdateMaxAutonomousRate: vi.fn(),
  maxGlobalAutonomousTasksPerHour: 50,
  onUpdateMaxGlobalAutonomousRate: vi.fn(),
  maxPipelineSlots: 4,
  onUpdateMaxPipelineSlots: vi.fn(),
  reservedSlotsForTypes: { create: 1, research: 1 },
  onUpdateReservedSlotsForTypes: vi.fn(),
  evaluatingPipelineTimeoutMinutes: 120,
  onUpdateEvaluatingPipelineTimeout: vi.fn(),
  evaluatingFailHours: 48,
  onUpdateEvaluatingFailHours: vi.fn(),
  ciBackstopTimeoutMinutes: 60,
  onUpdateCiBackstopTimeout: vi.fn(),
  autonomousResearchIntervalMinutes: 30,
  onUpdateAutonomousResearchInterval: vi.fn(),
  directionCooldownMinutes: 240,
  onUpdateDirectionCooldownMinutes: vi.fn(),
  strategicResearchFrequency: 0,
  onUpdateStrategicResearchFrequency: vi.fn(),
  anyAutonomousEnabled: true,
  onPauseAllAutonomous: vi.fn(),
  onRestartOrchestrator: vi.fn().mockResolvedValue(undefined),
  directionChurnWindowCycles: 5,
  onUpdateDirectionChurnWindowCycles: vi.fn(),
  directionChurnNonMergeThreshold: 15,
  onUpdateDirectionChurnNonMergeThreshold: vi.fn(),
  directionChurnMinMergeRate: 0.10,
  onUpdateDirectionChurnMinMergeRate: vi.fn(),
  directionLifetimeNonMergeThreshold: 150,
  onUpdateDirectionLifetimeNonMergeThreshold: vi.fn(),
  directionLifetimeMinMergeRate: 0.10,
  onUpdateDirectionLifetimeMinMergeRate: vi.fn(),
  directionReviewRecommendationThreshold: 2,
  onUpdateDirectionReviewRecommendationThreshold: vi.fn(),
};

describe('SettingsOrchestratorSection', () => {
  it('renders structural health observability panel with mode toggle', async () => {
    render(<SettingsOrchestratorSection {...baseProps} />);
    // Panel should be rendered
    const panel = screen.getByTestId('structural-health-observability-panel');
    expect(panel).toBeTruthy();
  });

  it('renders_metrics_integrity_observability_panel', () => {
    render(<SettingsOrchestratorSection {...baseProps} />);
    const panel = screen.getByTestId('metrics-integrity-observability-panel');
    expect(panel).toBeTruthy();
    expect(panel.textContent).toContain('Metrics Integrity Observability');
  });

  it('renders metrics integrity results and recent flags from the poller payload', async () => {
    vi.mocked(fetchMetricsIntegrityObservability).mockResolvedValueOnce({
      ticks: 7,
      checks_run: 19,
      flags_total: 2,
      errors_total: 1,
      enrolled_total: 3,
      last_tick_at: '2026-07-05T12:00:00Z',
      last_tick: null,
      recent_ticks: [],
      recent_flags: [
        { check_id: 'check-a', metric: 'cpu', expected: '< 0.8', found: '0.92' },
        { check_id: 'check-b', metric: 'memory', expected: '< 0.7', found: '0.81' },
      ],
    });

    render(<SettingsOrchestratorSection {...baseProps} />);

    const panel = screen.getByTestId('metrics-integrity-observability-panel');
    expect(await within(panel).findByText('7 ticks · 2 flags')).toBeInTheDocument();
    expect(within(panel).getByText('Checks Run').parentElement).toHaveTextContent('19');
    expect(within(panel).getByText('Errors Total').parentElement).toHaveTextContent('1');
    expect(within(panel).getByText('check-b')).toBeInTheDocument();
    expect(within(panel).getByText('memory')).toBeInTheDocument();
    expect(within(panel).queryByText('No drift flags recorded in recent ticks.')).not.toBeInTheDocument();
  });

  it('renders the empty-state message when the metrics poller has no ticks or flags', async () => {
    vi.mocked(fetchMetricsIntegrityObservability).mockResolvedValueOnce({
      ticks: 0,
      checks_run: 0,
      flags_total: 0,
      errors_total: 0,
      enrolled_total: 0,
      last_tick_at: null,
      last_tick: null,
      recent_ticks: [],
      recent_flags: [],
    });

    render(<SettingsOrchestratorSection {...baseProps} />);

    const panel = screen.getByTestId('metrics-integrity-observability-panel');
    expect(await within(panel).findByText('No metrics integrity sweep ticks recorded yet.')).toBeInTheDocument();
    expect(within(panel).queryByText('No drift flags recorded in recent ticks.')).not.toBeInTheDocument();
  });

  it('renders the live fleet proof-coverage meter from the settings payload', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce({
      ok: true,
      json: vi.fn().mockResolvedValue({
        structural_health_audit_mode: 'off',
        metrics_integrity_sweep_mode: 'off',
        capability_proof_gate_mode: 'off',
        capability_proof_gate_auto_graduation_enabled: false,
        capability_proof_gate_auto_graduation_observe_threshold: 0.5,
        capability_proof_gate_auto_graduation_enforce_threshold: 0.95,
        capability_proof_gate_coverage: {
          coverage_ratio: 0.5,
          total_proven: 1,
          total_non_exempt_working: 2,
          projects_contributing: 1,
          num_projects: 1,
        },
      }),
    } as unknown as Response);

    render(<SettingsOrchestratorSection {...baseProps} />);

    const value = await screen.findByTestId('proof-gate-fleet-coverage-value');
    expect(value).toHaveTextContent('1/2 (50%)');
  });

  it('renders n/a in the coverage meter when the ratio is undefined', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce({
      ok: true,
      json: vi.fn().mockResolvedValue({
        structural_health_audit_mode: 'off',
        metrics_integrity_sweep_mode: 'off',
        capability_proof_gate_mode: 'off',
        capability_proof_gate_auto_graduation_enabled: false,
        capability_proof_gate_auto_graduation_observe_threshold: 0.5,
        capability_proof_gate_auto_graduation_enforce_threshold: 0.95,
        capability_proof_gate_coverage: {
          coverage_ratio: null,
          total_proven: 0,
          total_non_exempt_working: 0,
          projects_contributing: 0,
          num_projects: 1,
        },
      }),
    } as unknown as Response);

    render(<SettingsOrchestratorSection {...baseProps} />);

    const meter = await screen.findByTestId('proof-gate-fleet-coverage-meter');
    expect(meter).toHaveTextContent('n/a');
    expect(screen.queryByTestId('proof-gate-fleet-coverage-value')).not.toBeInTheDocument();
  });

  it('patches the metrics integrity sweep mode when a mode button is clicked', async () => {
    const user = userEvent.setup();
    render(<SettingsOrchestratorSection {...baseProps} />);

    const panel = screen.getByTestId('metrics-integrity-observability-panel');
    await user.click(within(panel).getByRole('button', { name: 'enforce' }));

    await waitFor(() => {
      expect(patchMetricsIntegritySettings).toHaveBeenCalledWith({ metrics_integrity_sweep_mode: 'enforce' });
    });
  });

  it('calls onUpdateMaxVMs when max VMs input is blurred with a valid value', () => {
    const onUpdateMaxVMs = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateMaxVMs={onUpdateMaxVMs} />);
    const input = screen.getByLabelText('Max Concurrent VMs (0-100)');
    fireEvent.change(input, { target: { value: '8' } });
    fireEvent.blur(input);
    expect(onUpdateMaxVMs).toHaveBeenCalledWith(8);
  });

  it('reverts input to prop value on invalid blur', () => {
    const onUpdateMaxVMs = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} maxConcurrentVMs={4} onUpdateMaxVMs={onUpdateMaxVMs} />);
    const input = screen.getByLabelText('Max Concurrent VMs (0-100)') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '999' } });
    fireEvent.blur(input);
    expect(onUpdateMaxVMs).not.toHaveBeenCalled();
    expect(input.value).toBe('4');
  });

  it('calls onUpdateMaxRetries when pipeline cap input is blurred', () => {
    const onUpdateMaxRetries = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateMaxRetries={onUpdateMaxRetries} />);
    const input = screen.getByLabelText('Max Retries (0-10)');
    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.blur(input);
    expect(onUpdateMaxRetries).toHaveBeenCalledWith(5);
  });

  it('calls onUpdateMaxGlobalAutonomousRate when global rate input is blurred', () => {
    const onUpdateMaxGlobalAutonomousRate = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateMaxGlobalAutonomousRate={onUpdateMaxGlobalAutonomousRate} />);
    const input = screen.getByLabelText('Global Autonomous Tasks/Hour (1-500)');
    fireEvent.change(input, { target: { value: '200' } });
    fireEvent.blur(input);
    expect(onUpdateMaxGlobalAutonomousRate).toHaveBeenCalledWith(200);
  });

  it('calls onUpdateMaxPipelineSlots when pipeline slots input is blurred', () => {
    const onUpdateMaxPipelineSlots = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateMaxPipelineSlots={onUpdateMaxPipelineSlots} />);
    const input = screen.getByLabelText('Max Pipeline Slots (0-100)');
    fireEvent.change(input, { target: { value: '8' } });
    fireEvent.blur(input);
    expect(onUpdateMaxPipelineSlots).toHaveBeenCalledWith(8);
  });

  it('sends a complete reserved-slots dict without dropping keys when one type is committed', () => {
    const onUpdateReservedSlotsForTypes = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} reservedSlotsForTypes={{ create: 1, research: 1 }} onUpdateReservedSlotsForTypes={onUpdateReservedSlotsForTypes} />);
    const input = screen.getByLabelText('Reserved: plan (0-10)');
    fireEvent.change(input, { target: { value: '2' } });
    fireEvent.blur(input);
    expect(onUpdateReservedSlotsForTypes).toHaveBeenCalledWith({ create: 1, research: 1, plan: 2 });
  });

  it('renders 0 for a reserved type absent from the current map', () => {
    render(<SettingsOrchestratorSection {...baseProps} reservedSlotsForTypes={{ create: 1, research: 1 }} />);
    const input = screen.getByLabelText('Reserved: plan (0-10)') as HTMLInputElement;
    expect(input.value).toBe('0');
  });

  it('calls onUpdateEvaluatingPipelineTimeout when the evaluating timeout input is blurred', () => {
    const onUpdateEvaluatingPipelineTimeout = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateEvaluatingPipelineTimeout={onUpdateEvaluatingPipelineTimeout} />);
    const input = screen.getByLabelText('Evaluating Pipeline Timeout (1-1440 min)');
    fireEvent.change(input, { target: { value: '240' } });
    fireEvent.blur(input);
    expect(onUpdateEvaluatingPipelineTimeout).toHaveBeenCalledWith(240);
  });

  it('reverts the evaluating fail hours input to the prop value on invalid blur', () => {
    const onUpdateEvaluatingFailHours = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} evaluatingFailHours={48} onUpdateEvaluatingFailHours={onUpdateEvaluatingFailHours} />);
    const input = screen.getByLabelText('Evaluating Fail Hours (1-168)') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '999' } });
    fireEvent.blur(input);
    expect(onUpdateEvaluatingFailHours).not.toHaveBeenCalled();
    expect(input.value).toBe('48');
  });

  it('calls onUpdateEvaluatingFailHours when the evaluating fail hours input is blurred', () => {
    const onUpdateEvaluatingFailHours = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateEvaluatingFailHours={onUpdateEvaluatingFailHours} />);
    const input = screen.getByLabelText('Evaluating Fail Hours (1-168)');
    fireEvent.change(input, { target: { value: '72' } });
    fireEvent.blur(input);
    expect(onUpdateEvaluatingFailHours).toHaveBeenCalledWith(72);
  });

  it('calls onUpdateCiBackstopTimeout when CI backstop input is blurred', () => {
    const onUpdateCiBackstopTimeout = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateCiBackstopTimeout={onUpdateCiBackstopTimeout} />);
    const input = screen.getByLabelText('CI Backstop Timeout (1-1440 min)');
    fireEvent.change(input, { target: { value: '90' } });
    fireEvent.blur(input);
    expect(onUpdateCiBackstopTimeout).toHaveBeenCalledWith(90);
  });

  it('disables Pause All Autonomous when anyAutonomousEnabled is false', () => {
    render(<SettingsOrchestratorSection {...baseProps} anyAutonomousEnabled={false} />);
    expect(screen.getByText('Pause All Autonomous')).toBeDisabled();
  });

  it('calls onRestartOrchestrator when restart is confirmed', async () => {
    const onRestartOrchestrator = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<SettingsOrchestratorSection {...baseProps} onRestartOrchestrator={onRestartOrchestrator} />);
    fireEvent.click(screen.getByText('Restart Orchestrator'));
    expect(onRestartOrchestrator).toHaveBeenCalled();
  });

  it('does not call onRestartOrchestrator when confirm is cancelled', () => {
    const onRestartOrchestrator = vi.fn();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<SettingsOrchestratorSection {...baseProps} onRestartOrchestrator={onRestartOrchestrator} />);
    fireEvent.click(screen.getByText('Restart Orchestrator'));
    expect(onRestartOrchestrator).not.toHaveBeenCalled();
  });

  it('renders Direction Guardrails section with correct default values', () => {
    render(<SettingsOrchestratorSection {...baseProps} />);
    expect(screen.getByTestId('direction-guardrails-section')).toBeTruthy();
    expect((screen.getByTestId('direction-churn-window-cycles') as HTMLInputElement).value).toBe('5');
    expect((screen.getByTestId('direction-churn-non-merge-threshold') as HTMLInputElement).value).toBe('15');
    expect((screen.getByTestId('direction-lifetime-non-merge-threshold') as HTMLInputElement).value).toBe('150');
    expect((screen.getByTestId('direction-review-recommendation-threshold') as HTMLInputElement).value).toBe('2');
  });

  it('calls onUpdateDirectionChurnWindowCycles on valid blur', () => {
    const onUpdate = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateDirectionChurnWindowCycles={onUpdate} />);
    const input = screen.getByTestId('direction-churn-window-cycles');
    fireEvent.change(input, { target: { value: '8' } });
    fireEvent.blur(input);
    expect(onUpdate).toHaveBeenCalledWith(8);
  });

  it('calls onUpdateDirectionReviewRecommendationThreshold on valid blur', () => {
    const onUpdate = vi.fn();
    render(<SettingsOrchestratorSection {...baseProps} onUpdateDirectionReviewRecommendationThreshold={onUpdate} />);
    const input = screen.getByTestId('direction-review-recommendation-threshold');
    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.blur(input);
    expect(onUpdate).toHaveBeenCalledWith(5);
  });

  it('renders the Capability Proof Gate control group', () => {
    render(<SettingsOrchestratorSection {...baseProps} />);
    const section = screen.getByTestId('capability-proof-gate-section');
    expect(section).toBeTruthy();
    expect(section.textContent).toContain('Capability Proof Gate');
    const modeGroup = within(section).getByTestId('proof-gate-mode');
    expect(within(modeGroup).getByRole('button', { name: 'off' })).toBeInTheDocument();
    expect(within(modeGroup).getByRole('button', { name: 'observe' })).toBeInTheDocument();
    expect(within(modeGroup).getByRole('button', { name: 'enforce' })).toBeInTheDocument();
  });

  it('capability proof gate controls patch settings', async () => {
    const user = userEvent.setup();
    render(<SettingsOrchestratorSection {...baseProps} />);
    const section = screen.getByTestId('capability-proof-gate-section');

    // Wait for the settings fetch to seed the current-mode state so we can
    // observe the mode button reflecting 'off' before we change it.
    const modeGroup = within(section).getByTestId('proof-gate-mode');

    // Mode selector issues a partial PATCH of just the mode key.
    await user.click(within(modeGroup).getByRole('button', { name: 'enforce' }));
    await waitFor(() => {
      expect(patchCapabilityProofGateSettings).toHaveBeenCalledWith({ capability_proof_gate_mode: 'enforce' });
    });

    // Auto-graduation toggle issues a partial PATCH of just the boolean key.
    const autoGradContainer = within(section).getByTestId('proof-gate-auto-graduation');
    const toggle = within(autoGradContainer).getByRole('button', { name: '' });
    await user.click(toggle);
    await waitFor(() => {
      expect(patchCapabilityProofGateSettings).toHaveBeenCalledWith({ capability_proof_gate_auto_graduation_enabled: true });
    });

    // A valid threshold blur issues a partial PATCH of just that threshold key.
    const observe = within(section).getByTestId('proof-gate-observe-threshold');
    fireEvent.change(observe, { target: { value: '0.7' } });
    fireEvent.blur(observe);
    await waitFor(() => {
      expect(patchCapabilityProofGateSettings).toHaveBeenCalledWith({ capability_proof_gate_auto_graduation_observe_threshold: 0.7 });
    });
  });

  it('reverts an out-of-range proof-gate threshold without patching', async () => {
    render(<SettingsOrchestratorSection {...baseProps} />);
    const section = screen.getByTestId('capability-proof-gate-section');
    // Let the fetch settle so the input holds the seeded 0.95 default.
    const enforce = await within(section).findByTestId('proof-gate-enforce-threshold') as HTMLInputElement;
    await waitFor(() => expect(enforce.value).toBe('0.95'));
    vi.mocked(patchCapabilityProofGateSettings).mockClear();
    fireEvent.change(enforce, { target: { value: '2.5' } });
    fireEvent.blur(enforce);
    expect(patchCapabilityProofGateSettings).not.toHaveBeenCalled();
    expect(enforce.value).toBe('0.95');
  });
});
