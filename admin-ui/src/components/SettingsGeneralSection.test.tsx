import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SettingsGeneralSection } from './SettingsGeneralSection';

const baseProps = {
  apiHealthy: true,
  wsConnected: true,
  lastFetchTime: Date.now(),
  defaultTaskType: 'create' as const,
  onUpdateDefaultTaskType: vi.fn(),
  viewMode: 'kanban' as const,
  onSetViewMode: vi.fn(),
  allModelOptions: ['claude-sonnet-4-6', 'claude-opus-4-7'],
  refreshInterval: 30000,
  onUpdateRefreshInterval: vi.fn(),
  pageSize: 50,
  onUpdatePageSize: vi.fn(),
  notifyOnComplete: false,
  onUpdateNotifyOnComplete: vi.fn(),
  notifyOnFail: false,
  onUpdateNotifyOnFail: vi.fn(),
  notifyOnApprovalNeeded: false,
  onUpdateNotifyOnApprovalNeeded: vi.fn(),
  soundOnComplete: false,
  onUpdateSoundOnComplete: vi.fn(),
  notifyOnHighSignalMessage: false,
  onUpdateNotifyOnHighSignalMessage: vi.fn(),
  onNavigateToNotifications: vi.fn(),
};

describe('SettingsGeneralSection', () => {
  it('calls onUpdateRefreshInterval when a preset is clicked', () => {
    const onUpdateRefreshInterval = vi.fn();
    render(<SettingsGeneralSection {...baseProps} onUpdateRefreshInterval={onUpdateRefreshInterval} />);
    fireEvent.click(screen.getByText('10s'));
    expect(onUpdateRefreshInterval).toHaveBeenCalledWith(10000);
  });

  it('calls onUpdateRefreshInterval on custom interval blur', () => {
    const onUpdateRefreshInterval = vi.fn();
    render(<SettingsGeneralSection {...baseProps} onUpdateRefreshInterval={onUpdateRefreshInterval} />);
    const input = screen.getByPlaceholderText('Custom (s)');
    fireEvent.change(input, { target: { value: '45' } });
    fireEvent.blur(input);
    expect(onUpdateRefreshInterval).toHaveBeenCalledWith(45000);
  });

  it('calls onUpdatePageSize when a preset is clicked', () => {
    const onUpdatePageSize = vi.fn();
    render(<SettingsGeneralSection {...baseProps} onUpdatePageSize={onUpdatePageSize} />);
    fireEvent.click(screen.getByText('100'));
    expect(onUpdatePageSize).toHaveBeenCalledWith(100);
  });

  it('calls onUpdateNotifyOnComplete and requests permission when toggled on', () => {
    const onUpdateNotifyOnComplete = vi.fn();
    const requestPermission = vi.fn().mockResolvedValue('granted');
    Object.defineProperty(window, 'Notification', {
      writable: true,
      value: { permission: 'default', requestPermission },
    });
    render(<SettingsGeneralSection {...baseProps} onUpdateNotifyOnComplete={onUpdateNotifyOnComplete} />);
    const row = screen.getByText('Browser notification on task completion').closest('div');
    const toggle = row?.querySelector('button');
    if (toggle) fireEvent.click(toggle);
    expect(onUpdateNotifyOnComplete).toHaveBeenCalledWith(true);
    expect(requestPermission).toHaveBeenCalled();
  });

  it('calls onNavigateToNotifications when Notifications tab link is clicked', () => {
    const onNavigateToNotifications = vi.fn();
    render(<SettingsGeneralSection {...baseProps} onNavigateToNotifications={onNavigateToNotifications} />);
    fireEvent.click(screen.getByText('Notifications tab'));
    expect(onNavigateToNotifications).toHaveBeenCalled();
  });

  it('shows API error state when not healthy', () => {
    render(<SettingsGeneralSection {...baseProps} apiHealthy={false} />);
    expect(screen.getByText('API Error')).toBeInTheDocument();
  });

  it('renders onboarding progress when proof is provided', () => {
    const onboardingProof = {
      repo_added_at: '2024-01-01T00:00:00Z',
      bootstrap_completed_at: '2024-01-02T00:00:00Z',
      first_task_spawned_at: '2024-01-03T00:00:00Z',
      first_pr_opened_at: '2024-01-04T00:00:00Z',
      first_merge_gate_verdict_at: '2024-01-05T00:00:00Z',
      first_merge_gate_executed_tests: true,
      first_merge_gate_unverified_reason: null,
      first_pr_merged_at: '2024-01-06T00:00:00Z',
      first_autonomous_improvement_at: null,
    };
    render(<SettingsGeneralSection {...baseProps} selectedProjectOnboardingProof={onboardingProof} />);
    expect(screen.getByText('Onboarding Progress')).toBeInTheDocument();
    expect(screen.getByText('Repository Added')).toBeInTheDocument();
    expect(screen.getByText('Bootstrap Completed')).toBeInTheDocument();
    expect(screen.getByText('First Autonomous Improvement')).toBeInTheDocument();
  });

  it('does not render onboarding progress when proof is null', () => {
    render(<SettingsGeneralSection {...baseProps} selectedProjectOnboardingProof={null} />);
    expect(screen.queryByText('Onboarding Progress')).not.toBeInTheDocument();
  });
});
