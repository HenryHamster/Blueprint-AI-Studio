/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect, useCallback } from 'react';
import { X, Plus, Archive, ArchiveRestore, Trash2, RefreshCw, Loader2 } from 'lucide-react';
import { ModalShell } from './ui/ModalShell';
import { deleteProject, resetProject, archiveProject, unarchiveProject, fetchArchivedProjects } from '../api';
import type { Project } from '../types/project';

interface ProjectManagementModalProps {
  projects: Project[];
  selectedProjectId: string | null;
  onNewProject: () => void;
  onRefreshProjects: () => void;
  onSelectProject: (id: string | null) => void;
  onClose: () => void;
}

type Confirm = { type: 'delete' | 'reset'; projectId: string; projectLabel: string };

export function ProjectManagementModal({
  projects,
  selectedProjectId,
  onNewProject,
  onRefreshProjects,
  onSelectProject,
  onClose,
}: ProjectManagementModalProps) {
  const [tab, setTab] = useState<'active' | 'archived'>('active');
  const [archivedProjects, setArchivedProjects] = useState<Project[]>([]);
  const [archivedLoading, setArchivedLoading] = useState(false);
  const [archivedError, setArchivedError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const loadArchived = useCallback(async () => {
    setArchivedLoading(true);
    setArchivedError(null);
    try {
      const items = await fetchArchivedProjects();
      setArchivedProjects(items);
    } catch (e) {
      setArchivedError(e instanceof Error ? e.message : 'Failed to load archived projects');
    } finally {
      setArchivedLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tab === 'archived') loadArchived();
  }, [tab, loadArchived]);

  const handleArchive = async (projectId: string) => {
    setBusyId(projectId);
    setActionError(null);
    try {
      await archiveProject(projectId);
      if (selectedProjectId === projectId) onSelectProject(null);
      onRefreshProjects();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Failed to archive project');
    } finally {
      setBusyId(null);
    }
  };

  const handleUnarchive = async (projectId: string) => {
    setBusyId(projectId);
    setArchivedError(null);
    try {
      await unarchiveProject(projectId);
      setArchivedProjects(prev => prev.filter(p => p.id !== projectId));
      onRefreshProjects();
    } catch (e) {
      setArchivedError(e instanceof Error ? e.message : 'Failed to unarchive project');
    } finally {
      setBusyId(null);
    }
  };

  const handleConfirmedAction = async () => {
    if (!confirm) return;
    const { type, projectId } = confirm;
    setConfirm(null);
    setBusyId(projectId);
    setActionError(null);
    try {
      if (type === 'delete') {
        await deleteProject(projectId, true);
        if (selectedProjectId === projectId) onSelectProject(null);
        onRefreshProjects();
        setArchivedProjects(prev => prev.filter(p => p.id !== projectId));
      } else {
        await resetProject(projectId);
      }
    } catch (e) {
      setActionError(e instanceof Error ? e.message : `Failed to ${type} project`);
    } finally {
      setBusyId(null);
    }
  };

  const displayedProjects = tab === 'active' ? projects : archivedProjects;

  return (
    <ModalShell onClose={onClose} maxWidth="max-w-lg">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-neutral-100">Manage Projects</h2>
        <div className="flex items-center gap-2">
          <button
            onClick={() => { onClose(); onNewProject(); }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium bg-neutral-800 hover:bg-neutral-700 text-neutral-200 border border-neutral-700 transition-colors"
          >
            <Plus className="w-3 h-3" />
            New Project
          </button>
          {/* eslint-disable-next-line viewport/min-touch-target */}
          <button
            onClick={onClose}
            aria-label="Close"
            className="p-1.5 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      <div className="flex gap-1 mb-4 border-b border-neutral-800 pb-2">
        {(['active', 'archived'] as const).map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              tab === t
                ? 'bg-neutral-800 text-neutral-100'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            {t === 'active' ? `Active (${projects.length})` : 'Archived'}
          </button>
        ))}
      </div>

      {actionError && (
        <p className="text-xs text-red-400 mb-3 px-1" data-testid="action-error">{actionError}</p>
      )}

      {confirm && (
        <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg" data-testid="confirm-panel">
          <p className="text-xs text-neutral-300 mb-3">
            {confirm.type === 'delete'
              ? `Delete "${confirm.projectLabel}"? This cancels all active tasks and permanently removes all project data.`
              : `Reset "${confirm.projectLabel}"? This cancels all active tasks and deletes all task data. Project config is kept.`}
          </p>
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => setConfirm(null)}
              className="px-3 py-1.5 rounded text-xs font-medium bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleConfirmedAction}
              data-testid="confirm-action-btn"
              className="px-3 py-1.5 rounded text-xs font-medium bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/40 transition-colors"
            >
              {confirm.type === 'delete' ? 'Delete' : 'Reset'}
            </button>
          </div>
        </div>
      )}

      <div className="space-y-1 max-h-96 overflow-y-auto" data-testid="project-list">
        {tab === 'archived' && archivedLoading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-4 h-4 animate-spin text-neutral-500" />
          </div>
        )}
        {tab === 'archived' && archivedError && (
          <p className="text-xs text-red-400 text-center py-4" data-testid="archived-error">{archivedError}</p>
        )}
        {!archivedLoading && displayedProjects.length === 0 && (
          <p className="text-xs text-neutral-500 text-center py-6" data-testid="empty-state">
            {tab === 'active' ? 'No active projects' : 'No archived projects'}
          </p>
        )}
        {displayedProjects.map(project => {
          const label = `${project.github_owner}/${project.github_repo}`;
          const isBusy = busyId === project.id;
          const isConfirming = confirm?.projectId === project.id;
          return (
            <div
              key={project.id}
              data-testid={`project-row-${project.id}`}
              className={`flex items-center gap-2 px-3 py-2.5 rounded-lg bg-neutral-800/50 border ${
                selectedProjectId === project.id ? 'border-neutral-600' : 'border-transparent'
              }`}
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5 min-w-0">
                  <p className="text-xs font-medium text-neutral-200 truncate">{label}</p>
                  {tab === 'active' && project.paused && (
                    <span className="shrink-0 px-1.5 py-0.5 rounded text-ui-sm font-medium bg-amber-500/15 text-amber-400 border border-amber-500/20">
                      Paused
                    </span>
                  )}
                </div>
              </div>
              {isBusy ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin text-neutral-500 shrink-0" data-testid="busy-spinner" />
              ) : !isConfirming ? (
                <div className="flex items-center gap-1 shrink-0">
                  {tab === 'active' && (
                    <>
                      {/* eslint-disable-next-line viewport/min-touch-target */}
                      <button
                        onClick={() => setConfirm({ type: 'reset', projectId: project.id, projectLabel: label })}
                        title="Reset project"
                        data-testid={`reset-btn-${project.id}`}
                        className="p-1.5 rounded text-neutral-500 hover:text-amber-400 hover:bg-amber-500/10 transition-colors"
                      >
                        <RefreshCw className="w-3.5 h-3.5" />
                      </button>
                      {/* eslint-disable-next-line viewport/min-touch-target */}
                      <button
                        onClick={() => handleArchive(project.id)}
                        title="Archive project"
                        data-testid={`archive-btn-${project.id}`}
                        className="p-1.5 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700 transition-colors"
                      >
                        <Archive className="w-3.5 h-3.5" />
                      </button>
                    </>
                  )}
                  {tab === 'archived' && (
                    /* eslint-disable-next-line viewport/min-touch-target */
                    <button
                      onClick={() => handleUnarchive(project.id)}
                      title="Unarchive project"
                      data-testid={`unarchive-btn-${project.id}`}
                      className="p-1.5 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700 transition-colors"
                    >
                      <ArchiveRestore className="w-3.5 h-3.5" />
                    </button>
                  )}
                  {/* eslint-disable-next-line viewport/min-touch-target */}
                  <button
                    onClick={() => setConfirm({ type: 'delete', projectId: project.id, projectLabel: label })}
                    title="Delete project"
                    data-testid={`delete-btn-${project.id}`}
                    className="p-1.5 rounded text-neutral-500 hover:text-red-400 hover:bg-red-500/10 transition-colors"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </ModalShell>
  );
}
