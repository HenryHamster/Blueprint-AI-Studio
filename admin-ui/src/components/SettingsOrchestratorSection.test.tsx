import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SettingsOrchestratorSection } from './SettingsOrchestratorSection';

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
