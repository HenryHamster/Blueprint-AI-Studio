import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { ProjectWorkspace, type ProjectSection } from './OperatorWorkspaces';
import type { Project } from '../types/project';

vi.mock('./DirectionsPanel', () => ({
  DirectionsPanel: ({
    activeTab,
    readOnly,
    canManageGlobalSettings,
  }: {
    activeTab: string;
    readOnly?: boolean;
    canManageGlobalSettings?: boolean;
  }) => (
    <div
      data-testid="directions-panel"
      data-active-tab={activeTab}
      data-read-only={String(Boolean(readOnly))}
      data-can-manage-global={String(Boolean(canManageGlobalSettings))}
    />
  ),
}));

vi.mock('./SettingsSectionRouter', () => ({
  SettingsSectionRouter: () => <div data-testid="settings-router" />,
}));

vi.mock('./ProjectToolHost', () => ({
  ProjectToolHost: () => <div data-testid="project-tools" />,
}));

function makeProject(role: Project['my_role']): Project {
  return {
    id: 'project-1',
    name: 'Project One',
    status: 'active',
    github_owner: 'acme',
    github_repo: 'one',
    autonomous_enabled: false,
    created_at: '2026-08-02T00:00:00Z',
    my_role: role,
    is_self_repo: false,
  } as Project;
}

function props(
  role: Project['my_role'],
  options: { projectSection?: ProjectSection; isAdmin?: boolean } = {},
) {
  const project = makeProject(role);
  return {
    projectSection: options.projectSection ?? 'directions',
    onProjectSectionChange: vi.fn(),
    projectManageSection: 'overview' as const,
    onProjectManageSectionChange: vi.fn(),
    selectedProject: project,
    selectedProjectId: project.id,
    selectedProjectRepoLabel: 'acme/one',
    projects: [project],
    settingsRouterProps: { isAdmin: options.isAdmin ?? false } as never,
    addToast: vi.fn(),
    onOpenProjectManagement: vi.fn(),
    onViewFindings: vi.fn(),
    onViewKnowledge: vi.fn(),
    onNavigateToTask: vi.fn(),
  };
}

describe('ProjectWorkspace project-role boundaries', () => {
  test('viewer keeps read surfaces but has no Manage or project-management actions', () => {
    render(<ProjectWorkspace {...props('viewer')} />);

    const tabs = screen.getByLabelText('Project sections');
    expect(within(tabs).getByRole('button', { name: 'Directions' })).toBeInTheDocument();
    expect(within(tabs).getByRole('button', { name: 'Model' })).toBeInTheDocument();
    expect(within(tabs).getByRole('button', { name: 'Design' })).toBeInTheDocument();
    expect(within(tabs).queryByRole('button', { name: 'Manage' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Projects' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Members' })).not.toBeInTheDocument();
    expect(screen.getByTestId('directions-panel')).toHaveAttribute('data-read-only', 'true');
    expect(screen.getByTestId('directions-panel')).toHaveAttribute('data-can-manage-global', 'false');
  });

  test('member retains operational collaboration while owner alone gets Manage', () => {
    const { rerender } = render(<ProjectWorkspace {...props('member')} />);

    expect(screen.getByTestId('directions-panel')).toHaveAttribute('data-read-only', 'false');
    expect(
      within(screen.getByLabelText('Project sections')).queryByRole('button', { name: 'Manage' }),
    ).not.toBeInTheDocument();

    rerender(<ProjectWorkspace {...props('owner', { isAdmin: true })} />);
    expect(
      within(screen.getByLabelText('Project sections')).getByRole('button', { name: 'Manage' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Projects' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Members' })).toBeInTheDocument();
    expect(screen.getByTestId('directions-panel')).toHaveAttribute('data-can-manage-global', 'true');
  });

  test('Members shortcut opens the existing Manage Access section', () => {
    const ownerProps = props('owner');
    render(<ProjectWorkspace {...ownerProps} />);

    fireEvent.click(screen.getByRole('button', { name: 'Members' }));

    expect(ownerProps.onProjectManageSectionChange).toHaveBeenCalledWith('access');
    expect(ownerProps.onProjectSectionChange).toHaveBeenCalledWith('manage');
  });

  test('Access receives focus and supports Escape and visible back navigation', () => {
    const ownerProps = {
      ...props('owner', { projectSection: 'manage' }),
      projectManageSection: 'access' as const,
    };
    render(<ProjectWorkspace {...ownerProps} />);

    expect(screen.getByRole('heading', { name: 'Access' })).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: 'Back to project management' }));

    expect(ownerProps.onProjectManageSectionChange).toHaveBeenNthCalledWith(1, 'overview');
    expect(ownerProps.onProjectManageSectionChange).toHaveBeenNthCalledWith(2, 'overview');
  });

  test('stale viewer Manage selection falls back to a read surface without mounting settings', () => {
    render(<ProjectWorkspace {...props('viewer', { projectSection: 'manage' })} />);

    expect(screen.queryByTestId('settings-router')).not.toBeInTheDocument();
    expect(screen.getByTestId('directions-panel')).toHaveAttribute('data-active-tab', 'directions');
  });
});
