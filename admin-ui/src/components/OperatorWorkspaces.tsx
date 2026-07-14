import { lazy, Suspense, type MutableRefObject } from 'react';
import {
  Users,
  Wrench,
} from 'lucide-react';
import { ProjectToolHost } from './ProjectToolHost';
import { DirectionsPanel } from './DirectionsPanel';
import type { SettingsSectionRouterProps } from './SettingsSectionRouter';
const SettingsSectionRouter = lazy(() => import('./SettingsSectionRouter').then(m => ({ default: m.SettingsSectionRouter })));
import { WorkbenchPanel, WorkbenchSurface } from './WorkbenchLayout';
import {
  PROJECT_MANAGE_SECTIONS,
  resolveProjectManageItem,
  type ProjectManageSection,
  type ProjectSection,
} from './OperatorWorkspaceSections';
import type { StrategicCycleCompletion, StrategicCycleProgress } from '../hooks/useLogViewer';
import type { Project } from '../types/project';

export type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from './OperatorWorkspaceSections';

function ProjectNoSelectionState({
  onOpenProjectManagement,
  onProjectSectionChange,
}: {
  onOpenProjectManagement: () => void;
  onProjectSectionChange: (section: ProjectSection) => void;
}) {
  return (
    <div className="flex h-full min-h-0 items-center justify-center p-4">
      <WorkbenchPanel title="Select project" meta="required" className="w-full max-w-lg">
        <div className="space-y-3 text-sm text-neutral-400">
          <p>No project is selected for this workspace.</p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onOpenProjectManagement}
              className="inline-flex min-h-11 items-center gap-2 rounded-md border border-neutral-700 bg-neutral-900 px-3 text-sm font-semibold text-neutral-200 transition-colors hover:bg-neutral-800"
            >
              <Wrench className="h-3.5 w-3.5" />
              Manage projects
            </button>
            <button
              type="button"
              onClick={() => onProjectSectionChange('manage')}
              className="inline-flex min-h-11 items-center rounded-md border border-neutral-800 px-3 text-sm font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900"
            >
              Project overview
            </button>
          </div>
        </div>
      </WorkbenchPanel>
    </div>
  );
}

interface ProjectWorkspaceProps {
  projectSection: ProjectSection;
  onProjectSectionChange: (section: ProjectSection) => void;
  projectManageSection: ProjectManageSection;
  onProjectManageSectionChange: (section: ProjectManageSection) => void;
  selectedProject: Project | null;
  selectedProjectId: string | null;
  selectedProjectRepoLabel: string | null;
  projects: Project[];
  settingsRouterProps: Omit<SettingsSectionRouterProps, 'activeSection'>;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  fetchDirectionsRef?: MutableRefObject<() => void>;
  strategistActive?: boolean;
  cycleProgress?: StrategicCycleProgress | null;
  lastCycleCompletion?: StrategicCycleCompletion | null;
  onOpenProjectManagement: () => void;
  onOpenProjectMembers: () => void;
  onViewFindings: (directionId: string) => void;
  onViewKnowledge: (directionId: string) => void;
  onNavigateToTask: (taskId: string) => void;
}

export function ProjectWorkspace({
  projectSection,
  onProjectSectionChange,
  projectManageSection,
  onProjectManageSectionChange,
  selectedProject,
  selectedProjectId,
  selectedProjectRepoLabel,
  projects,
  settingsRouterProps,
  addToast,
  fetchDirectionsRef,
  strategistActive,
  cycleProgress,
  lastCycleCompletion,
  onOpenProjectManagement,
  onOpenProjectMembers,
  onViewFindings,
  onViewKnowledge,
  onNavigateToTask,
}: ProjectWorkspaceProps) {
  const activeProjectManageItem = resolveProjectManageItem(projectManageSection);
  const ActiveProjectManageIcon = activeProjectManageItem.icon;
  const activeProjectSettingsSection = activeProjectManageItem.id === 'overview' ? null : activeProjectManageItem.id;
  const actionButtonClass = 'workbench-focus inline-flex min-h-11 items-center gap-1.5 rounded-md border border-workbench-border bg-workbench-layer px-3 text-xs font-semibold text-neutral-300 transition-colors hover:border-workbench-border-strong hover:bg-workbench-hover hover:text-neutral-100 sm:min-h-9';

  const projectSurfaceActions = (
    <>
      <button
        type="button"
        onClick={onOpenProjectManagement}
        className={actionButtonClass}
      >
        <Wrench className="h-3.5 w-3.5" />
        Projects
      </button>
      {selectedProjectId && (
        <button
          type="button"
          onClick={onOpenProjectMembers}
          className={actionButtonClass}
        >
          <Users className="h-3.5 w-3.5" />
          Members
        </button>
      )}
    </>
  );

  return (
    <WorkbenchSurface
      title="Project"
      subtitle={selectedProjectRepoLabel ?? 'Select a project to manage durable context'}
      tabs={[
        { id: 'running-update', label: 'Update' },
        { id: 'directions', label: 'Directions' },
        { id: 'problem-model', label: 'Model' },
        { id: 'design-doc', label: 'Design' },
        { id: 'manage', label: 'Manage' },
        { id: 'tools', label: 'Tools' },
      ]}
      activeTab={projectSection}
      onTabChange={onProjectSectionChange}
      actions={projectSurfaceActions}
      tabLayout="stacked"
      flush
      contentClassName="min-h-0 min-w-0 overflow-hidden"
    >
      {projectSection === 'tools' ? (
        selectedProjectId ? (
          <div className="min-h-0 overflow-auto p-4 custom-scrollbar">
            <ProjectToolHost projectId={selectedProjectId} myRole={selectedProject?.my_role} />
          </div>
        ) : (
          <ProjectNoSelectionState
            onOpenProjectManagement={onOpenProjectManagement}
            onProjectSectionChange={onProjectSectionChange}
          />
        )
      ) : projectSection !== 'manage' && !selectedProjectId ? (
        <ProjectNoSelectionState
          onOpenProjectManagement={onOpenProjectManagement}
          onProjectSectionChange={onProjectSectionChange}
        />
      ) : projectSection !== 'manage' ? (
        <DirectionsPanel
          addToast={addToast}
          selectedProjectId={selectedProjectId}
          projects={projects}
          fetchDirectionsRef={fetchDirectionsRef}
          strategistActive={strategistActive}
          cycleProgress={cycleProgress}
          lastCycleCompletion={lastCycleCompletion}
          activeTab={projectSection}
          onActiveTabChange={onProjectSectionChange}
          showHeaderTitle={false}
          showInternalTabs={false}
          onViewFindings={onViewFindings}
          onViewKnowledge={onViewKnowledge}
          onNavigateToTask={onNavigateToTask}
        />
      ) : (
        <div className="grid h-full min-h-0 grid-cols-1 overflow-hidden lg:grid-cols-[280px_minmax(0,1fr)]">
          <aside className="min-h-0 overflow-auto border-b border-workbench-border bg-workbench-rail p-3 custom-scrollbar lg:border-b-0 lg:border-r">
            <WorkbenchPanel title="Project" meta={selectedProject?.my_role ?? 'none'}>
              <div className="space-y-3 text-sm text-neutral-400">
                <div className="min-w-0">
                  <div className="text-ui-md uppercase tracking-wide text-neutral-500">repository</div>
                  <div className="mt-1 truncate font-medium text-neutral-200">{selectedProjectRepoLabel ?? 'No project selected'}</div>
                </div>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div className="workbench-card rounded border px-2 py-1.5">
                    <div className="text-ui-md uppercase tracking-wide text-neutral-500">role</div>
                    <div className="mt-0.5 text-neutral-300">{selectedProject?.my_role ?? 'none'}</div>
                  </div>
                  <div className="workbench-card rounded border px-2 py-1.5">
                    <div className="text-ui-md uppercase tracking-wide text-neutral-500">self repo</div>
                    <div className="mt-0.5 text-neutral-300">{selectedProject?.is_self_repo ? 'yes' : 'no'}</div>
                  </div>
                </div>
              </div>
            </WorkbenchPanel>

            <nav className="mt-3 space-y-1" aria-label="Project management sections">
              {PROJECT_MANAGE_SECTIONS.map(item => {
                const Icon = item.icon;
                const selected = item.id === projectManageSection;
                const disabled = item.id !== 'overview' && !selectedProjectId;
                return (
                  <button
                    key={item.id}
                    type="button"
                    disabled={disabled}
                    onClick={() => onProjectManageSectionChange(item.id)}
                    className={`flex w-full items-start gap-2 rounded-md border px-2.5 py-2 text-left transition-colors ${
                      selected
                        ? 'border-workbench-border-strong bg-neutral-800 text-neutral-100'
                        : 'border-transparent text-neutral-400 hover:border-workbench-border hover:bg-workbench-layer/70 hover:text-neutral-200'
                    } disabled:cursor-not-allowed disabled:opacity-40`}
                  >
                    <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span className="min-w-0">
                      <span className="block truncate text-xs font-medium">{item.label}</span>
                      <span className="mt-0.5 block text-ui-md leading-snug text-neutral-500">{item.description}</span>
                    </span>
                  </button>
                );
              })}
            </nav>
          </aside>

          <div className="min-h-0 overflow-auto p-4 custom-scrollbar">
            {projectManageSection === 'overview' ? (
              <div className="mx-auto grid max-w-6xl gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(340px,0.95fr)]">
                <WorkbenchPanel title="Repository">
                  <div className="space-y-3 text-sm text-neutral-400">
                    <div>
                      <div className="text-ui-md uppercase tracking-wide text-neutral-500">selected project</div>
                      <div className="mt-1 text-base font-semibold text-neutral-100">
                        {selectedProject?.name ?? selectedProjectRepoLabel ?? 'No project selected'}
                      </div>
                      {selectedProjectRepoLabel && <div className="mt-0.5 text-xs text-neutral-500">{selectedProjectRepoLabel}</div>}
                    </div>
                  </div>
                </WorkbenchPanel>

                <WorkbenchPanel title="Strategy" meta={strategistActive ? 'running' : 'idle'}>
                  <div className="space-y-3 text-sm text-neutral-400">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-neutral-500">cycle</span>
                      <span className={strategistActive ? 'text-indigo-300' : 'text-neutral-300'}>
                        {strategistActive ? (cycleProgress?.phase_label ?? 'working') : 'idle'}
                      </span>
                    </div>
                    {lastCycleCompletion?.status_message && (
                      <p className="text-xs leading-relaxed text-neutral-400">{lastCycleCompletion.status_message.slice(0, 220)}</p>
                    )}
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => onProjectSectionChange('running-update')}
                        className="min-h-11 rounded-md border border-neutral-700 px-3 py-2 text-xs font-medium text-neutral-200 transition-colors hover:bg-neutral-800 sm:min-h-9"
                      >
                        Open update
                      </button>
                      <button
                        type="button"
                        onClick={() => onProjectSectionChange('directions')}
                        className="min-h-11 rounded-md border border-neutral-700 px-3 py-2 text-xs font-medium text-neutral-200 transition-colors hover:bg-neutral-800 sm:min-h-9"
                      >
                        Open directions
                      </button>
                    </div>
                  </div>
                </WorkbenchPanel>

                <WorkbenchPanel title="Project settings" className="xl:col-span-2">
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                    {PROJECT_MANAGE_SECTIONS.filter(item => item.id !== 'overview').map(item => {
                      const Icon = item.icon;
                      return (
                        <button
                          key={item.id}
                          type="button"
                          disabled={!selectedProjectId}
                          onClick={() => onProjectManageSectionChange(item.id)}
                          className="workbench-card flex min-h-[78px] items-start gap-2 rounded-md border p-3 text-left text-neutral-300 transition-colors hover:border-workbench-border-strong hover:bg-workbench-layer disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          <Icon className="mt-0.5 h-4 w-4 shrink-0 text-neutral-500" />
                          <span className="min-w-0">
                            <span className="block text-xs font-semibold">{item.label}</span>
                            <span className="mt-1 block text-ui-md leading-snug text-neutral-500">{item.description}</span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </WorkbenchPanel>
              </div>
            ) : (
              <div className="mx-auto max-w-6xl space-y-4">
                <div className="flex min-w-0 items-start gap-3 border-b border-neutral-800 pb-3">
                  <span className="workbench-layer flex h-9 w-9 shrink-0 items-center justify-center rounded-md border text-neutral-300">
                    <ActiveProjectManageIcon className="h-4 w-4" />
                  </span>
                  <div className="min-w-0">
                    <h2 className="text-sm font-semibold text-neutral-100">{activeProjectManageItem.label}</h2>
                    <p className="mt-1 text-xs text-neutral-500">{activeProjectManageItem.description}</p>
                  </div>
                </div>
                {activeProjectSettingsSection && (
                  <Suspense fallback={null}>
                    <SettingsSectionRouter {...settingsRouterProps} hostKind="workspace" activeSection={activeProjectSettingsSection} />
                  </Suspense>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </WorkbenchSurface>
  );
}
