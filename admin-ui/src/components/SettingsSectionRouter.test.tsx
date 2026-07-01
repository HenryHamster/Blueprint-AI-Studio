import { describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { SettingsSectionRouter, type SettingsSectionRouterProps } from './SettingsSectionRouter';

function accessProps(overrides: Partial<SettingsSectionRouterProps> = {}): SettingsSectionRouterProps {
  return {
    activeSection: 'access',
    isAdmin: true,
    selectedProjectId: 'project-1',
    selectedProjectMyRole: 'owner',
    showUserManagement: false,
    onToggleUserManagement: vi.fn(),
    showProjectMembers: false,
    onToggleProjectMembers: vi.fn(),
    onToggleSettings: vi.fn(),
    ...overrides,
  } as SettingsSectionRouterProps;
}

describe('SettingsSectionRouter access actions', () => {
  test('closes the modal host by default when opening project members', () => {
    const onToggleProjectMembers = vi.fn();
    const onToggleSettings = vi.fn();

    render(
      <SettingsSectionRouter
        {...accessProps({ onToggleProjectMembers, onToggleSettings })}
      />,
    );

    fireEvent.click(screen.getByTestId('toggle-project-members'));

    expect(onToggleProjectMembers).toHaveBeenCalledOnce();
    expect(onToggleSettings).toHaveBeenCalledOnce();
  });

  test('keeps embedded project/runtime hosts open when opening user management', () => {
    const onToggleUserManagement = vi.fn();
    const onToggleSettings = vi.fn();

    render(
      <SettingsSectionRouter
        {...accessProps({
          closeOnExternalPanelOpen: false,
          onToggleUserManagement,
          onToggleSettings,
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Manage Users' }));

    expect(onToggleUserManagement).toHaveBeenCalledOnce();
    expect(onToggleSettings).not.toHaveBeenCalled();
  });

  test('hides project member management when no project is selected', () => {
    render(
      <SettingsSectionRouter
        {...accessProps({ selectedProjectId: null })}
      />,
    );

    expect(screen.queryByTestId('toggle-project-members')).toBeNull();
    expect(screen.getByRole('button', { name: 'Manage Users' })).toBeInTheDocument();
  });

  test('hides project member management for non-owner project members', () => {
    render(
      <SettingsSectionRouter
        {...accessProps({
          isAdmin: false,
          selectedProjectMyRole: 'member',
        })}
      />,
    );

    expect(screen.queryByTestId('toggle-project-members')).toBeNull();
    expect(screen.getByText('No access management options are available for your current role.')).toBeInTheDocument();
  });
});
