import { describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { SettingsSectionRouter, type SettingsSectionRouterProps } from './SettingsSectionRouter';

function accessProps(overrides: Partial<SettingsSectionRouterProps> = {}): SettingsSectionRouterProps {
  return {
    activeSection: 'access',
    isAdmin: true,
    selectedProjectId: 'project-1',
    selectedProjectRole: 'owner',
    showUserManagement: false,
    onToggleUserManagement: vi.fn(),
    projectMembers: [],
    projectMembersProjectId: 'project-1',
    onLoadProjectMembers: vi.fn(),
    onActivateProjectMembers: vi.fn(),
    onAddProjectMember: vi.fn(),
    onUpdateProjectMemberRole: vi.fn(),
    onRemoveProjectMember: vi.fn(),
    onToggleSettings: vi.fn(),
    ...overrides,
  } as SettingsSectionRouterProps;
}

describe('SettingsSectionRouter access actions', () => {
  test('embeds project member management in Access and loads the selected project', () => {
    const onLoadProjectMembers = vi.fn();

    render(
      <SettingsSectionRouter
        {...accessProps({ onLoadProjectMembers })}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Project Members' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Invite' })).toBeInTheDocument();
    expect(onLoadProjectMembers).toHaveBeenCalledWith('project-1');
  });

  test('deactivates project member state when leaving Access', () => {
    const onActivateProjectMembers = vi.fn();

    const { unmount } = render(
      <SettingsSectionRouter
        {...accessProps({ onActivateProjectMembers })}
      />,
    );

    unmount();

    expect(onActivateProjectMembers).toHaveBeenCalledWith(null);
  });

  test('resets invitation state when the selected project changes', () => {
    const onLoadProjectMembers = vi.fn();
    const { rerender } = render(
      <SettingsSectionRouter
        {...accessProps({ onLoadProjectMembers })}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText('user@example.com'), {
      target: { value: 'project-one@example.com' },
    });

    rerender(
      <SettingsSectionRouter
        {...accessProps({
          selectedProjectId: 'project-2',
          projectMembersProjectId: 'project-2',
          onLoadProjectMembers,
        })}
      />,
    );

    expect(screen.getByPlaceholderText('user@example.com')).toHaveValue('');
    expect(onLoadProjectMembers).toHaveBeenLastCalledWith('project-2');
  });

  test('keeps explicit workspace hosts open when opening user management', () => {
    const onToggleUserManagement = vi.fn();
    const onToggleSettings = vi.fn();

    render(
      <SettingsSectionRouter
        {...accessProps({
          hostKind: 'workspace',
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

    expect(screen.queryByRole('heading', { name: 'Project Members' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Manage Users' })).toBeInTheDocument();
  });

  test('hides project member management for non-owner project members', () => {
    const onLoadProjectMembers = vi.fn();
    render(
      <SettingsSectionRouter
        {...accessProps({
          isAdmin: false,
          selectedProjectRole: 'member',
          onLoadProjectMembers,
        })}
      />,
    );

    expect(screen.queryByRole('heading', { name: 'Project Members' })).toBeNull();
    expect(screen.getByText('No access management options are available for your current role.')).toBeInTheDocument();
    expect(onLoadProjectMembers).not.toHaveBeenCalled();
  });

  test('does not let platform admin status substitute for project ownership', () => {
    render(
      <SettingsSectionRouter
        {...accessProps({
          isAdmin: true,
          selectedProjectRole: 'member',
        })}
      />,
    );

    expect(screen.queryByRole('heading', { name: 'Project Members' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Manage Users' })).toBeInTheDocument();
  });
});
