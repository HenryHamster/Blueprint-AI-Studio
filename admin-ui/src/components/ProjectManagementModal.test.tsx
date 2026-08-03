import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectManagementModal } from './ProjectManagementModal';
import type { Project } from '../types/project';

vi.mock('../api', () => ({
  fetchArchivedProjects: vi.fn(async () => []),
  archiveProject: vi.fn(async () => ({ message: 'ok', archived: true })),
  unarchiveProject: vi.fn(async () => ({ message: 'ok', archived: false })),
  deleteProject: vi.fn(async () => ({ message: 'ok', active_tasks_cancelled: 0 })),
  resetProject: vi.fn(async () => ({ message: 'ok', active_tasks_cancelled: 0, tasks_deleted: 0 })),
}));

import * as api from '../api';

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'owner/repo',
    status: 'active',
    github_owner: 'owner',
    github_repo: 'repo',
    autonomous_enabled: false,
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

let container: HTMLDivElement;
let root: Root;

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

function render(ui: React.ReactElement) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(ui));
  return container;
}

const defaultProps = {
  projects: [makeProject()],
  selectedProjectId: null,
  canManageProject: () => true,
  onNewProject: vi.fn(),
  onRefreshProjects: vi.fn(),
  onSelectProject: vi.fn(),
  onClose: vi.fn(),
};

describe('ProjectManagementModal', () => {
  test('renders active project list', async () => {
    const el = render(<ProjectManagementModal {...defaultProps} />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="project-row-proj-1"]')).not.toBeNull();
    expect(el.textContent).toContain('owner/repo');
  });

  test('does not render the raw lifecycle status enum string', async () => {
    // Project.status is a deprecated, meaningless-to-operators value (autonomous
    // projects sit at DEPLOYING forever). It must not be surfaced as a subtitle.
    const el = render(
      <ProjectManagementModal
        {...defaultProps}
        projects={[makeProject({ status: 'DEPLOYING' })]}
      />,
    );
    await act(async () => {});
    expect(el.textContent).not.toContain('DEPLOYING');
  });

  test('shows a Paused chip for a paused project', async () => {
    const el = render(
      <ProjectManagementModal
        {...defaultProps}
        projects={[makeProject({ paused: true })]}
      />,
    );
    await act(async () => {});
    const row = el.querySelector('[data-testid="project-row-proj-1"]');
    expect(row?.textContent).toContain('Paused');
  });

  test('shows no Paused chip for an un-paused project', async () => {
    const el = render(
      <ProjectManagementModal
        {...defaultProps}
        projects={[makeProject({ paused: false })]}
      />,
    );
    await act(async () => {});
    const row = el.querySelector('[data-testid="project-row-proj-1"]');
    expect(row?.textContent).not.toContain('Paused');
  });

  test('shows empty state when no active projects', async () => {
    const el = render(<ProjectManagementModal {...defaultProps} projects={[]} />);
    await act(async () => {});
    const empty = el.querySelector('[data-testid="empty-state"]');
    expect(empty?.textContent).toContain('No active projects');
  });

  test('archive button calls archiveProject and refreshes', async () => {
    const onRefresh = vi.fn();
    const el = render(<ProjectManagementModal {...defaultProps} onRefreshProjects={onRefresh} />);
    await act(async () => {});
    const btn = el.querySelector('[data-testid="archive-btn-proj-1"]') as HTMLButtonElement;
    await act(async () => { btn.click(); });
    expect(api.archiveProject).toHaveBeenCalledWith('proj-1');
    expect(onRefresh).toHaveBeenCalled();
  });

  test('delete button shows confirm panel then calls deleteProject', async () => {
    const onRefresh = vi.fn();
    const el = render(<ProjectManagementModal {...defaultProps} onRefreshProjects={onRefresh} />);
    await act(async () => {});
    const deleteBtn = el.querySelector('[data-testid="delete-btn-proj-1"]') as HTMLButtonElement;
    await act(async () => { deleteBtn.click(); });
    const confirmPanel = el.querySelector('[data-testid="confirm-panel"]');
    expect(confirmPanel).not.toBeNull();
    const confirmBtn = el.querySelector('[data-testid="confirm-action-btn"]') as HTMLButtonElement;
    await act(async () => { confirmBtn.click(); });
    expect(api.deleteProject).toHaveBeenCalledWith('proj-1', true);
    expect(onRefresh).toHaveBeenCalled();
  });

  test('reset button shows confirm panel then calls resetProject', async () => {
    const el = render(<ProjectManagementModal {...defaultProps} />);
    await act(async () => {});
    const resetBtn = el.querySelector('[data-testid="reset-btn-proj-1"]') as HTMLButtonElement;
    await act(async () => { resetBtn.click(); });
    expect(el.querySelector('[data-testid="confirm-panel"]')).not.toBeNull();
    const confirmBtn = el.querySelector('[data-testid="confirm-action-btn"]') as HTMLButtonElement;
    await act(async () => { confirmBtn.click(); });
    expect(api.resetProject).toHaveBeenCalledWith('proj-1');
  });

  test('cancel confirm returns to normal state', async () => {
    const el = render(<ProjectManagementModal {...defaultProps} />);
    await act(async () => {});
    const deleteBtn = el.querySelector('[data-testid="delete-btn-proj-1"]') as HTMLButtonElement;
    await act(async () => { deleteBtn.click(); });
    expect(el.querySelector('[data-testid="confirm-panel"]')).not.toBeNull();
    const cancelBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Cancel') as HTMLButtonElement;
    await act(async () => { cancelBtn.click(); });
    expect(el.querySelector('[data-testid="confirm-panel"]')).toBeNull();
    expect(el.querySelector('[data-testid="delete-btn-proj-1"]')).not.toBeNull();
  });

  test('archived tab fetches archived projects and shows unarchive button', async () => {
    const archivedProject = makeProject({ id: 'proj-arch', github_repo: 'archived-repo', archived: true });
    vi.mocked(api.fetchArchivedProjects).mockResolvedValueOnce([archivedProject]);
    const el = render(<ProjectManagementModal {...defaultProps} />);
    await act(async () => {});
    const archivedTab = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Archived')) as HTMLButtonElement;
    await act(async () => { archivedTab.click(); });
    await act(async () => {});
    expect(api.fetchArchivedProjects).toHaveBeenCalled();
    expect(el.querySelector('[data-testid="unarchive-btn-proj-arch"]')).not.toBeNull();
  });

  test('unarchive button calls unarchiveProject and removes project from list', async () => {
    const archivedProject = makeProject({ id: 'proj-arch', github_repo: 'archived-repo', archived: true });
    vi.mocked(api.fetchArchivedProjects).mockResolvedValueOnce([archivedProject]);
    const onRefresh = vi.fn();
    const el = render(<ProjectManagementModal {...defaultProps} onRefreshProjects={onRefresh} />);
    await act(async () => {});
    const archivedTab = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Archived')) as HTMLButtonElement;
    await act(async () => { archivedTab.click(); });
    await act(async () => {});
    const unarchiveBtn = el.querySelector('[data-testid="unarchive-btn-proj-arch"]') as HTMLButtonElement;
    await act(async () => { unarchiveBtn.click(); });
    expect(api.unarchiveProject).toHaveBeenCalledWith('proj-arch');
    expect(onRefresh).toHaveBeenCalled();
    expect(el.querySelector('[data-testid="project-row-proj-arch"]')).toBeNull();
  });

  test('New Project button closes modal and triggers onNewProject', async () => {
    const onClose = vi.fn();
    const onNewProject = vi.fn();
    const el = render(<ProjectManagementModal {...defaultProps} onClose={onClose} onNewProject={onNewProject} />);
    await act(async () => {});
    const newBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('New Project')) as HTMLButtonElement;
    await act(async () => { newBtn.click(); });
    expect(onClose).toHaveBeenCalled();
    expect(onNewProject).toHaveBeenCalled();
  });

  test.each(['member', 'viewer'] as const)('hides owner-only controls for a %s while keeping New Project', async (role) => {
    const el = render(
      <ProjectManagementModal
        {...defaultProps}
        projects={[makeProject({ my_role: role })]}
        canManageProject={() => false}
      />,
    );
    await act(async () => {});

    expect(el.querySelector('[data-testid="reset-btn-proj-1"]')).toBeNull();
    expect(el.querySelector('[data-testid="archive-btn-proj-1"]')).toBeNull();
    expect(el.querySelector('[data-testid="delete-btn-proj-1"]')).toBeNull();
    expect(Array.from(el.querySelectorAll('button')).some(button => button.textContent?.includes('New Project'))).toBe(true);
  });

  test('unknown access fails closed for owner-only controls', async () => {
    const el = render(<ProjectManagementModal {...defaultProps} canManageProject={() => false} />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="reset-btn-proj-1"]')).toBeNull();
    expect(el.querySelector('[data-testid="archive-btn-proj-1"]')).toBeNull();
    expect(el.querySelector('[data-testid="delete-btn-proj-1"]')).toBeNull();
  });

  test('shows error message when archiveProject fails', async () => {
    vi.mocked(api.archiveProject).mockRejectedValueOnce(new Error('Permission denied'));
    const el = render(<ProjectManagementModal {...defaultProps} />);
    await act(async () => {});
    const archiveBtn = el.querySelector('[data-testid="archive-btn-proj-1"]') as HTMLButtonElement;
    await act(async () => { archiveBtn.click(); });
    expect(el.querySelector('[data-testid="action-error"]')?.textContent).toBe('Permission denied');
  });
});
