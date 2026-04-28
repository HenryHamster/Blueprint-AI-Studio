import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ProjectDashboard } from './ProjectDashboard';
import type { Project } from '../types/project';
import type { Task } from './TaskCard';

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'Platform',
    status: 'active',
    github_owner: 'acme',
    github_repo: 'platform',
    autonomous_enabled: true,
    directions: [{ id: 'd-1', text: 'Ship the retry fix', priority: 'high', status: 'active', added_at: '2026-04-24T00:00:00Z' }],
    created_at: '2026-04-23T10:00:00Z',
    ...overrides,
  };
}

function makeTask(project_id: string, status: Task['status']): Task {
  return {
    id: `${project_id}-${status}`,
    project_id,
    title: status,
    description: status,
    status,
    created_by: 'agent',
    requires_approval: false,
    created_at: '2026-04-24T11:00:00Z',
  };
}

describe('ProjectDashboard', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-24T12:00:00Z'));
    localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('returns null when there are no projects', () => {
    const { container } = render(
      <ProjectDashboard projects={[]} tasks={[]} selectedProjectId={null} onSelectProject={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders rows, counts, and project actions when expanded', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    const onSelectProject = vi.fn();
    const onToggleMembers = vi.fn();
    const onNewProject = vi.fn();
    const onOpenSetupGuide = vi.fn();

    render(
      <ProjectDashboard
        projects={[makeProject(), makeProject({ id: 'proj-2', name: 'Bootstrap', github_repo: 'bootstrap', autonomous_enabled: false, created_at: '2026-04-24T11:30:00Z', directions: [] })]}
        tasks={[
          makeTask('proj-1', 'QUEUED'),
          makeTask('proj-1', 'IN_PROGRESS'),
          makeTask('proj-1', 'EVALUATING'),
          makeTask('proj-1', 'FAILED'),
          makeTask('proj-1', 'COMPLETED'),
        ]}
        selectedProjectId="proj-1"
        onSelectProject={onSelectProject}
        showMembers
        onToggleMembers={onToggleMembers}
        userRole="admin"
        onNewProject={onNewProject}
        onOpenSetupGuide={onOpenSetupGuide}
      />,
    );

    expect(screen.getByText('Projects')).toBeInTheDocument();
    expect(screen.getByText('(filtered)')).toBeInTheDocument();
    expect(screen.getByText('Platform')).toBeInTheDocument();
    expect(screen.getByText('acme/platform')).toBeInTheDocument();
    expect(screen.getByText('1 queued')).toBeInTheDocument();
    expect(screen.getByText('1 running')).toBeInTheDocument();
    expect(screen.getByText('1 evaluating')).toBeInTheDocument();
    expect(screen.getAllByText('1 failed')).toHaveLength(2);
    expect(screen.getByText('1 done')).toBeInTheDocument();
    expect(screen.getByText('Bootstrap')).toBeInTheDocument();
    expect(screen.getByTitle('Complete project setup')).toBeInTheDocument();
    expect(screen.getByTitle('Project Members')).toBeInTheDocument();
    expect(screen.getByTitle('Add new project')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Platform' }));
    expect(onSelectProject).toHaveBeenCalledWith(null);
    fireEvent.click(screen.getByTitle('Project Members'));
    expect(onToggleMembers).toHaveBeenCalled();
    fireEvent.click(screen.getByTitle('Complete project setup'));
    expect(onOpenSetupGuide).toHaveBeenCalledWith('proj-2');
    fireEvent.click(screen.getByTitle('Add new project'));
    expect(onNewProject).toHaveBeenCalled();
  });

  it('hides the admin new-project control for non-admin users', () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');
    render(
      <ProjectDashboard
        projects={[makeProject()]}
        tasks={[]}
        selectedProjectId={null}
        onSelectProject={vi.fn()}
        userRole="user"
      />,
    );

    expect(screen.queryByTitle('Add new project')).not.toBeInTheDocument();
  });
});
