import { useState, type ComponentProps, type MutableRefObject, type ReactNode } from 'react';
import {
  RotateCw,
  Users,
  Wrench,
} from 'lucide-react';
import { ProjectToolHost } from './ProjectToolHost';
import { DirectionsPanel } from './DirectionsPanel';
import { ComputeJobsPanel } from './ComputeJobsPanel';
import { DashboardHome } from './DashboardHome';
import { ConsoleHome } from './ConsoleHome';
import { CompletionDebtPanel } from './CompletionDebtPanel';
import { FleetRunnersPanel } from './FleetRunnersPanel';
import { RuntimeAccountsPanel } from './RuntimeAccountsPanel';
import { SettingsSectionRouter, type SettingsSectionRouterProps } from './SettingsSectionRouter';
import type { Task } from './TaskCard';
import { WarmVmPanel } from './WarmVmPanel';
import { VersionLifecyclePanel } from './VersionLifecyclePanel';
import { WorkbenchPanel, WorkbenchSurface } from './WorkbenchLayout';
import type { FailingTaskTypeEntry } from '../types/diagnostics';
import { fetchAgentServingShas, type AgentServingShasResponse, type VersionLifecycleResponse } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { hasVersionLifecycleData } from '../hooks/useVersionLifecycle';
import {
  PROJECT_MANAGE_SECTIONS,
  RUNTIME_SECTIONS,
  resolveProjectManageItem,
  resolveRuntimeItem,
  resolveRuntimeSettingsItem,
  visibleRuntimeSettingsSections,
  type ProjectManageSection,
  type ProjectSection,
  type RuntimeSection,
  type RuntimeSettingsSection,
  type SectionNavItem,
} from './OperatorWorkspaceSections';
import type { StrategicCycleCompletion, StrategicCycleProgress } from '../hooks/useLogViewer';
import type { ComputeExperimentForm } from '../hooks/useTaskModal';
import type { Project } from '../types/project';

export type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from './OperatorWorkspaceSections';

function RuntimeSectionIntro<T extends string>({
  item,
  title = item.label,
  children,
}: {
  item: SectionNavItem<T>;
  title?: string;
  children?: ReactNode;
}) {
  const Icon = item.icon;
  return (
    <div className="flex min-w-0 flex-col gap-2 border-b border-workbench-border/80 pb-3 md:flex-row md:items-end md:justify-between">
      <div className="flex min-w-0 items-start gap-3">
        <span className="workbench-layer flex h-9 w-9 shrink-0 items-center justify-center rounded-md border text-neutral-300">
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-neutral-100">{title}</h2>
        </div>
      </div>
      {children && (
        <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-1.5 md:justify-end">
          {children}
        </div>
      )}
    </div>
  );
}

function RuntimeStatusPill({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: ReactNode;
  tone?: 'neutral' | 'good' | 'warn' | 'info';
}) {
  const valueClass = {
    neutral: 'text-neutral-200',
    good: 'text-emerald-300',
    warn: 'text-amber-300',
    info: 'text-cyan-300',
  }[tone];

  return (
    <div className="workbench-card inline-flex min-h-7 max-w-full items-center gap-1.5 rounded-md border px-2 text-ui-md">
      <span className="shrink-0 uppercase tracking-wide text-neutral-500">{label}</span>
      <span className={`min-w-0 truncate font-mono font-semibold ${valueClass}`}>{value}</span>
    </div>
  );
}

function RuntimeSettingsStatusRail({
  selectedProjectRepoLabel,
  selectedProjectId,
  apiHealthy,
  wsConnected,
  userRole,
}: {
  selectedProjectRepoLabel: string | null;
  selectedProjectId: string | null;
  apiHealthy: boolean;
  wsConnected: boolean;
  userRole: 'admin' | 'user' | null;
}) {
  const rows = [
    {
      label: 'Scope',
      value: selectedProjectRepoLabel ?? (selectedProjectId ? 'Project selected' : 'Fleet-wide'),
      tone: selectedProjectId ? 'text-cyan-300' : 'text-neutral-300',
    },
    {
      label: 'API',
      value: apiHealthy ? 'connected' : 'degraded',
      tone: apiHealthy ? 'text-emerald-300' : 'text-amber-300',
    },
    {
      label: 'Events',
      value: wsConnected ? 'connected' : 'stale',
      tone: wsConnected ? 'text-emerald-300' : 'text-amber-300',
    },
    {
      label: 'Role',
      value: userRole ?? 'unknown',
      tone: userRole === 'admin' ? 'text-neutral-100' : 'text-neutral-300',
    },
  ];

  return (
    <div data-testid="runtime-settings-status">
      <WorkbenchPanel title="Runtime" meta={apiHealthy && wsConnected ? 'live' : 'check'}>
        <dl className="grid gap-1.5 text-xs">
          {rows.map(row => (
            <div key={row.label} className="workbench-card grid min-w-0 grid-cols-[4.4rem_minmax(0,1fr)] gap-2 rounded border px-2 py-1.5">
              <dt className="font-semibold uppercase tracking-wide text-neutral-500">{row.label}</dt>
              <dd className={`min-w-0 truncate font-mono ${row.tone}`} title={row.value}>{row.value}</dd>
            </div>
          ))}
        </dl>
      </WorkbenchPanel>
    </div>
  );
}

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
            <ProjectToolHost projectId={selectedProjectId} />
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
                  <SettingsSectionRouter {...settingsRouterProps} hostKind="workspace" activeSection={activeProjectSettingsSection} />
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </WorkbenchSurface>
  );
}

type DashboardHomeProps = ComponentProps<typeof DashboardHome>;

interface RuntimeWorkspaceProps {
  runtimeSection: RuntimeSection;
  onRuntimeSectionChange: (section: RuntimeSection) => void;
  runtimeConfigSection: RuntimeSettingsSection;
  onRuntimeConfigSectionChange: (section: RuntimeSettingsSection) => void;
  selectedProjectId: string | null;
  selectedProjectRepoLabel: string | null;
  projects: Project[];
  tasks: Task[];
  scopedActiveCount?: number | null;
  scopedQueuedCount?: number | null;
  globalActiveCount: number;
  globalQueuedCount: number;
  userRole: 'admin' | 'user' | null;
  apiHealthy: boolean;
  wsConnected: boolean;
  settingsRouterProps: Omit<SettingsSectionRouterProps, 'activeSection'>;
  onViewTask: (task: Task) => void;
  onViewTaskById: (taskId: string) => void;
  onOpenFleet: () => void;
  onCreateTask: () => void;
  onLaunchCompute: () => void;
  onCloneExperiment?: (form: ComputeExperimentForm) => void;
  onDirectionClick: (directionId: string) => void;
  onConfigureTargets: () => void;
  onSpawnOrphanFix: DashboardHomeProps['onSpawnOrphanFix'];
  onPanelReviewPr: DashboardHomeProps['onPanelReviewPr'];
  gatedTaskTypes?: FailingTaskTypeEntry[];
  onApproveExperiment: (taskId: string) => Promise<void>;
  onDismissExperiment: (taskId: string) => Promise<void>;
  onNavigateToKnowledge: (documentId: string) => void;
  onRestartOrchestrator: () => Promise<void>;
  fleetRunnersRefreshSignal?: number;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  versionLifecycle: VersionLifecycleResponse | null;
  versionLifecycleError: string | null;
  versionLifecycleLastSuccessAt: string | null;
  versionLifecycleRefreshing: boolean;
  refreshVersionLifecycle: () => Promise<void>;
}

export function RuntimeWorkspace({
  runtimeSection,
  onRuntimeSectionChange,
  runtimeConfigSection,
  onRuntimeConfigSectionChange,
  selectedProjectId,
  selectedProjectRepoLabel,
  projects,
  tasks,
  scopedActiveCount,
  scopedQueuedCount,
  globalActiveCount,
  globalQueuedCount,
  userRole,
  apiHealthy,
  wsConnected,
  settingsRouterProps,
  onViewTask,
  onViewTaskById,
  onOpenFleet,
  onCreateTask,
  onLaunchCompute,
  onCloneExperiment,
  onDirectionClick,
  onConfigureTargets,
  onSpawnOrphanFix,
  onPanelReviewPr,
  gatedTaskTypes,
  onApproveExperiment,
  onDismissExperiment,
  onNavigateToKnowledge,
  onRestartOrchestrator,
  fleetRunnersRefreshSignal,
  addToast,
  versionLifecycle,
  versionLifecycleError,
  versionLifecycleLastSuccessAt,
  versionLifecycleRefreshing,
  refreshVersionLifecycle,
}: RuntimeWorkspaceProps) {
  const runtimeSettingsSections = visibleRuntimeSettingsSections(userRole);
  const activeRuntimeItem = resolveRuntimeItem(runtimeSection);
  const activeRuntimeSettingsItem = resolveRuntimeSettingsItem(runtimeConfigSection, userRole);
  const hasVersionData = hasVersionLifecycleData(versionLifecycle);
  const [agentServingShas, setAgentServingShas] = useState<AgentServingShasResponse | null>(null);
  usePoller(async (signal) => {
    if (!selectedProjectId) { setAgentServingShas(null); return; }
    try {
      const data = await fetchAgentServingShas(selectedProjectId);
      if (!signal.aborted) setAgentServingShas(data);
    } catch {
      if (!signal.aborted) setAgentServingShas(null);
    }
  }, { intervalMs: DEFAULT_POLL_MS, deps: [selectedProjectId] });
  const visibleRuntimeSections = RUNTIME_SECTIONS.filter(
    s => s.id !== 'version-lifecycle' || hasVersionData,
  );
  const actionButtonClass = 'inline-flex min-h-11 min-w-11 items-center justify-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-0 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 sm:min-h-9 sm:min-w-0 sm:px-3';
  const runtimeAccounts = settingsRouterProps.maxPlanAccounts ?? [];
  const accountTotal = runtimeAccounts.length;
  const enabledAccounts = runtimeAccounts.filter(account => account.enabled).length;
  const activeAccountTasks = runtimeAccounts.reduce((sum, account) => sum + (account.active_tasks ?? 0), 0);
  const cappedAccounts = runtimeAccounts.filter(account => account.daily_task_cap != null || account.max_concurrent_tasks != null).length;
  const disabledModelCount = runtimeAccounts.reduce(
    (sum, account) => sum + Object.keys(account.disabled_models ?? {}).length,
    0,
  );

  const runtimeSurfaceActions = (
    <>
      <span className="workbench-layer hidden min-h-9 items-center gap-1.5 rounded-md border px-2.5 text-xs text-neutral-400 md:inline-flex">
        <span className={`h-1.5 w-1.5 rounded-full ${apiHealthy && wsConnected ? 'bg-emerald-400' : 'bg-amber-400'}`} />
        {apiHealthy && wsConnected ? 'API + WS' : 'degraded'}
      </span>
      {userRole === 'admin' && (
        <button
          type="button"
          onClick={() => {
            // The handler toasts on failure and re-throws; swallow the rejection
            // here so a failed restart doesn't surface as an unhandledrejection.
            void onRestartOrchestrator().catch(() => {});
          }}
          className={actionButtonClass}
          title="Restart orchestrator"
          aria-label="Restart orchestrator"
        >
          <RotateCw className="h-3.5 w-3.5" />
          <span className="hidden lg:inline">Restart</span>
        </button>
      )}
    </>
  );

  return (
    <WorkbenchSurface
      title="Runtime"
      subtitle="Overview, devices, compute, accounts, settings"
      tabs={visibleRuntimeSections.map(({ id, label }) => ({ id, label }))}
      activeTab={runtimeSection}
      onTabChange={onRuntimeSectionChange}
      actions={runtimeSurfaceActions}
      tabLayout="stacked"
      flush
      contentClassName={runtimeSection === 'settings' ? 'min-h-0 min-w-0 overflow-hidden' : 'min-h-0 min-w-0 overflow-auto p-3 custom-scrollbar'}
    >
      {runtimeSection === 'health' ? (
        <div className="mx-auto flex max-w-7xl flex-col gap-3">
          <RuntimeSectionIntro
            item={activeRuntimeItem}
            title="Runtime overview"
          >
            <RuntimeStatusPill label="API" value={apiHealthy ? 'connected' : 'error'} tone={apiHealthy ? 'good' : 'warn'} />
            <RuntimeStatusPill label="WS" value={wsConnected ? 'connected' : 'stale'} tone={wsConnected ? 'good' : 'warn'} />
            <RuntimeStatusPill
              label="project"
              value={selectedProjectRepoLabel ?? 'fleet-wide'}
              tone={selectedProjectId ? 'info' : 'neutral'}
            />
            <RuntimeStatusPill
              label="fleet"
              value={`${globalActiveCount} active / ${globalQueuedCount} queued`}
              tone={globalActiveCount > 0 ? 'info' : 'neutral'}
            />
          </RuntimeSectionIntro>
          <ConsoleHome
            selectedProjectId={selectedProjectId}
            scopedActiveCount={scopedActiveCount}
            scopedQueuedCount={scopedQueuedCount}
            globalActiveCount={globalActiveCount}
            globalQueuedCount={globalQueuedCount}
            tasks={tasks}
            onViewTask={onViewTask}
            onShowAnalytics={() => onRuntimeSectionChange('analytics')}
            onOpenFleet={onOpenFleet}
            onCreateTask={onCreateTask}
          />
          {selectedProjectId && (
            <CompletionDebtPanel
              projectId={selectedProjectId}
              addToast={addToast}
            />
          )}
        </div>
      ) : runtimeSection === 'devices' ? (
        <div className="mx-auto flex max-w-7xl flex-col gap-3">
          <RuntimeSectionIntro
            item={activeRuntimeItem}
            title="Devices and runners"
          >
            <RuntimeStatusPill
              label="project"
              value={selectedProjectRepoLabel ?? 'none'}
              tone={selectedProjectId ? 'info' : 'warn'}
            />
            <RuntimeStatusPill label="VMs" value="warm pool" tone="good" />
            <RuntimeStatusPill
              label="runners"
              value={selectedProjectId ? 'project scoped' : 'select project'}
              tone={selectedProjectId ? 'good' : 'warn'}
            />
          </RuntimeSectionIntro>
          <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(360px,420px)]">
            <section className="workbench-layer min-w-0 rounded-md border p-3" aria-label="Warm VM devices">
              <WarmVmPanel onViewTask={onViewTaskById} projects={projects} />
            </section>
            <section className="workbench-layer min-w-0 rounded-md border p-3" aria-label="Self-hosted runner devices">
              {selectedProjectId ? (
                <FleetRunnersPanel
                  projectId={selectedProjectId}
                  requireLocalRunner={settingsRouterProps.resourceEnvelopeData?.require_local_runner ?? false}
                  refreshSignal={fleetRunnersRefreshSignal}
                  live={wsConnected}
                />
              ) : (
                <div className="py-10 text-center text-xs text-neutral-500">
                  Select a project to manage self-hosted runners.
                </div>
              )}
            </section>
          </div>
        </div>
      ) : runtimeSection === 'compute' ? (
        <div className="mx-auto flex max-w-7xl flex-col gap-3">
          <RuntimeSectionIntro
            item={activeRuntimeItem}
            title="Compute jobs"
          >
            <RuntimeStatusPill
              label="project"
              value={`${scopedActiveCount ?? 0} active / ${scopedQueuedCount ?? 0} queued`}
              tone={(scopedActiveCount ?? 0) > 0 ? 'info' : 'neutral'}
            />
            <RuntimeStatusPill
              label="fleet"
              value={`${globalActiveCount} active / ${globalQueuedCount} queued`}
              tone={globalActiveCount > 0 ? 'info' : 'neutral'}
            />
          </RuntimeSectionIntro>
          <ComputeJobsPanel
            projectId={selectedProjectId}
            onNavigateToTask={onViewTaskById}
            onLaunchCompute={onLaunchCompute}
            onCloneExperiment={onCloneExperiment}
            onApproveExperiment={onApproveExperiment}
            onDismissExperiment={onDismissExperiment}
            onNavigateToKnowledge={onNavigateToKnowledge}
          />
        </div>
      ) : runtimeSection === 'analytics' ? (
        <DashboardHome
          tasks={tasks}
          selectedProjectId={selectedProjectId}
          projects={projects}
          globalActiveCount={globalActiveCount}
          globalQueuedCount={globalQueuedCount}
          scopedActiveCount={scopedActiveCount}
          scopedQueuedCount={scopedQueuedCount}
          onViewTask={onViewTask}
          onDirectionClick={onDirectionClick}
          onConfigureTargets={onConfigureTargets}
          onSpawnOrphanFix={onSpawnOrphanFix}
          onPanelReviewPr={onPanelReviewPr}
          gatedTaskTypes={gatedTaskTypes}
          userRole={userRole}
        />
      ) : runtimeSection === 'version-lifecycle' ? (
        <div className="mx-auto max-w-6xl space-y-4">
          {selectedProjectId ? (
            <VersionLifecyclePanel
              data={versionLifecycle}
              loading={false}
              projectId={selectedProjectId}
              onRefresh={refreshVersionLifecycle}
              userRole={userRole}
              refreshError={versionLifecycleError}
              lastSuccessfulRefreshAt={versionLifecycleLastSuccessAt}
              refreshing={versionLifecycleRefreshing}
              agentServingShas={agentServingShas}
            />
          ) : (
            <div className="py-10 text-center text-xs text-neutral-500">
              Select a project to view version lifecycle.
            </div>
          )}
        </div>
      ) : runtimeSection === 'accounts' ? (
        <div className="mx-auto flex max-w-6xl flex-col gap-3">
          <RuntimeSectionIntro
            item={activeRuntimeItem}
            title="Accounts and usage"
          >
            <RuntimeStatusPill
              label="GitHub"
              value={settingsRouterProps.githubAccount ? 'connected' : 'not connected'}
              tone={settingsRouterProps.githubAccount ? 'good' : 'warn'}
            />
            <RuntimeStatusPill
              label="LLM"
              value={`${enabledAccounts}/${accountTotal} enabled`}
              tone={enabledAccounts > 0 ? 'good' : 'warn'}
            />
            <RuntimeStatusPill
              label="active"
              value={`${activeAccountTasks} tasks`}
              tone={activeAccountTasks > 0 ? 'info' : 'neutral'}
            />
            {accountTotal > 0 && (
              <RuntimeStatusPill
                label="caps"
                value={`${cappedAccounts} set`}
                tone={cappedAccounts > 0 ? 'info' : 'neutral'}
              />
            )}
            {disabledModelCount > 0 && (
              <RuntimeStatusPill label="cooldowns" value={disabledModelCount} tone="warn" />
            )}
          </RuntimeSectionIntro>
          <RuntimeAccountsPanel
            selectedProjectId={selectedProjectId}
            settingsRouterProps={settingsRouterProps}
            availableSettingsSections={runtimeSettingsSections.map(item => item.id)}
            onOpenRuntimeSettingsSection={(section) => {
              onRuntimeConfigSectionChange(section);
              onRuntimeSectionChange('settings');
            }}
          />
        </div>
      ) : (
        <div className="grid h-full min-h-0 grid-cols-1 overflow-hidden lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="min-h-0 overflow-auto border-b border-workbench-border bg-workbench-rail p-3 custom-scrollbar lg:border-b-0 lg:border-r">
            <RuntimeSettingsStatusRail
              selectedProjectRepoLabel={selectedProjectRepoLabel}
              selectedProjectId={selectedProjectId}
              apiHealthy={apiHealthy}
              wsConnected={wsConnected}
              userRole={userRole}
            />
            <nav className="mt-3 space-y-1" aria-label="Runtime settings sections">
              {runtimeSettingsSections.map(item => {
                const Icon = item.icon;
                const selected = item.id === activeRuntimeSettingsItem.id;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => onRuntimeConfigSectionChange(item.id)}
                    className={`flex min-h-11 w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left transition-colors ${
                      selected
                        ? 'border-workbench-border-strong bg-workbench-hover text-neutral-100'
                        : 'border-transparent text-neutral-400 hover:border-workbench-border hover:bg-workbench-layer hover:text-neutral-200'
                    }`}
                  >
                    <Icon className="h-3.5 w-3.5 shrink-0" />
                    <span className="min-w-0 flex-1 truncate text-xs font-medium">{item.label}</span>
                    {item.adminOnly && (
                      <span className="shrink-0 rounded border border-neutral-700 px-1.5 py-0.5 text-ui-sm uppercase tracking-wide text-neutral-500">
                        admin
                      </span>
                    )}
                  </button>
                );
              })}
            </nav>
          </aside>
          <div className="min-h-0 overflow-auto p-3 custom-scrollbar">
            <div className="mx-auto max-w-6xl space-y-4">
              <RuntimeSectionIntro item={activeRuntimeSettingsItem} />
              <SettingsSectionRouter {...settingsRouterProps} hostKind="workspace" activeSection={activeRuntimeSettingsItem.id} />
            </div>
          </div>
        </div>
      )}
    </WorkbenchSurface>
  );
}
