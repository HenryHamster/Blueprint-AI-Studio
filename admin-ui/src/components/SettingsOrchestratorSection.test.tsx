import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SettingsOrchestratorSection } from './SettingsOrchestratorSection';

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
  fetchEvergreenFillObservability: vi.fn().mockResolvedValue({
    created_count: 0,
    reason_counts: {},
    trigger_counts: {},
    recent: [],
  }),
  fetchEvergreenBacklog: vi.fn().mockResolvedValue({ items: [] }),
  fetchEvergreenBacklogSummary: vi.fn().mockResolvedValue({}),
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
};

describe('SettingsOrchestratorSection', () => {
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
});
