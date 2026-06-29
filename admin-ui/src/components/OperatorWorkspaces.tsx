import type { ComponentProps, MutableRefObject } from 'react';
import {
  Activity,
  Bell,
  Cpu,
  Gauge,
  GitBranch,
  RotateCw,
  Server,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Users,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import { DirectionsPanel } from './DirectionsPanel';
import type { DirectionsTab } from './DirectionsPanelHeader';
import { ComputeJobsPanel } from './ComputeJobsPanel';
import { DashboardHome } from './DashboardHome';
import { ConsoleHome } from './ConsoleHome';
import { FleetRunnersPanel } from './FleetRunnersPanel';
import { SettingsSectionRouter, type SettingsSectionRouterProps } from './SettingsSectionRouter';
import type { SectionId } from './SettingsDropdown';
import type { Task } from './TaskCard';
import { WarmVmPanel } from './WarmVmPanel';
import { WorkbenchPanel, WorkbenchSurface } from './WorkbenchLayout';
import type { StrategicCycleCompletion, StrategicCycleProgress } from '../hooks/useLogViewer';
import type { Project } from '../types/project';

export type ProjectSection = DirectionsTab | 'manage';
export type ProjectManageSection =
  | 'overview'
  | Extract<
      SectionId,
      | 'build-config'
      | 'branches'
      | 'auto-review'
      | 'resource-envelope'
      | 'auto-remediation'
      | 'issue-implementation'
      | 'alert-domains'
      | 'access'
    >;
export type RuntimeSection = 'health' | 'devices' | 'compute' | 'accounts' | 'settings' | 'analytics';
export type RuntimeSettingsSection = Extract<
  SectionId,
  | 'general'
  | 'orchestrator'
  | 'deploy'
  | 'provider-routing'
  | 'provider-settings'
  | 'notifications'
  | 'advanced'
>;

type SectionNavItem<T extends string> = {
  id: T;
  label: string;
  description: string;
  icon: LucideIcon;
  adminOnly?: boolean;
};

const PROJECT_MANAGE_SECTIONS: SectionNavItem<ProjectManageSection>[] = [
  { id: 'overview', label: 'Overview', description: 'Repository, role, and project actions.', icon: Activity },
  { id: 'build-config', label: 'Build', description: 'Detection, test, and install commands.', icon: Wrench },
  { id: 'branches', label: 'Branches', description: 'Integration and promotion targets.', icon: GitBranch },
  { id: 'auto-review', label: 'Auto-review', description: 'Review gates and autonomous decisions.', icon: Gauge },
  { id: 'resource-envelope', label: 'Resources', description: 'Project runtime and budget envelope.', icon: Server },
  { id: 'auto-remediation', label: 'Remediation', description: 'Failure recovery behavior.', icon: ShieldCheck },
  { id: 'issue-implementation', label: 'Issues', description: 'Issue implementation policy.', icon: Settings },
  { id: 'alert-domains', label: 'Alerts', description: 'Project alert domain routing.', icon: Bell },
  { id: 'access', label: 'Access', description: 'Project members and user access.', icon: Users },
];

const RUNTIME_SETTINGS_SECTIONS: SectionNavItem<RuntimeSettingsSection>[] = [
  { id: 'general', label: 'General', description: 'Defaults, refresh, and local UI behavior.', icon: SlidersHorizontal },
  { id: 'orchestrator', label: 'Orchestrator', description: 'Global slots, retries, and automation limits.', icon: Server },
  { id: 'deploy', label: 'Deploy', description: 'Pipeline health and deployment controls.', icon: ShieldCheck, adminOnly: true },
  { id: 'provider-routing', label: 'Provider routing', description: 'Task and category routing policy.', icon: GitBranch, adminOnly: true },
  { id: 'provider-settings', label: 'Provider settings', description: 'Model defaults and effort routing.', icon: Settings, adminOnly: true },
  { id: 'notifications', label: 'Notifications', description: 'Webhook, event, and spend alerts.', icon: Bell },
  { id: 'advanced', label: 'Advanced', description: 'Health-check endpoints and diagnostics.', icon: Wrench },
];

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
  const activeProjectManageItem =
    PROJECT_MANAGE_SECTIONS.find(item => item.id === projectManageSection) ?? PROJECT_MANAGE_SECTIONS[0]!;
  const ActiveProjectManageIcon = activeProjectManageItem.icon;
  const activeProjectSettingsSection = activeProjectManageItem.id === 'overview' ? null : activeProjectManageItem.id;
  const actionButtonClass = 'inline-flex min-h-9 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-3 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100';

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
      ]}
      activeTab={projectSection}
      onTabChange={onProjectSectionChange}
      actions={projectSurfaceActions}
      tabLayout="stacked"
      flush
      contentClassName="min-h-0 min-w-0 overflow-hidden"
    >
      {projectSection !== 'manage' ? (
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
          <aside className="min-h-0 overflow-auto border-b border-neutral-800 bg-[#0b0f14] p-3 custom-scrollbar lg:border-b-0 lg:border-r">
            <WorkbenchPanel title="Project" meta={selectedProject?.my_role ?? 'none'}>
              <div className="space-y-3 text-sm text-neutral-400">
                <div className="min-w-0">
                  <div className="text-ui-md uppercase tracking-wide text-neutral-500">repository</div>
                  <div className="mt-1 truncate font-medium text-neutral-200">{selectedProjectRepoLabel ?? 'No project selected'}</div>
                </div>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div className="rounded border border-neutral-800 bg-[#0d1218] px-2 py-1.5">
                    <div className="text-ui-md uppercase tracking-wide text-neutral-500">role</div>
                    <div className="mt-0.5 text-neutral-300">{selectedProject?.my_role ?? 'none'}</div>
                  </div>
                  <div className="rounded border border-neutral-800 bg-[#0d1218] px-2 py-1.5">
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
                        ? 'border-neutral-700 bg-neutral-800 text-neutral-100'
                        : 'border-transparent text-neutral-400 hover:border-neutral-800 hover:bg-neutral-900/70 hover:text-neutral-200'
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
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={onOpenProjectManagement}
                        className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-neutral-700 px-3 text-xs font-medium text-neutral-200 transition-colors hover:bg-neutral-800"
                      >
                        <Wrench className="h-3.5 w-3.5" />
                        Manage projects
                      </button>
                      {selectedProjectId && (
                        <button
                          type="button"
                          onClick={onOpenProjectMembers}
                          className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-neutral-700 px-3 text-xs font-medium text-neutral-200 transition-colors hover:bg-neutral-800"
                        >
                          <Users className="h-3.5 w-3.5" />
                          Members
                        </button>
                      )}
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
                        className="rounded-md border border-neutral-700 px-3 py-2 text-xs font-medium text-neutral-200 transition-colors hover:bg-neutral-800"
                      >
                        Open update
                      </button>
                      <button
                        type="button"
                        onClick={() => onProjectSectionChange('directions')}
                        className="rounded-md border border-neutral-700 px-3 py-2 text-xs font-medium text-neutral-200 transition-colors hover:bg-neutral-800"
                      >
                        Open directions
                      </button>
                    </div>
                  </div>
                </WorkbenchPanel>

                <WorkbenchPanel title="Project settings" className="xl:col-span-2">
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                    {PROJECT_MANAGE_SECTIONS.filter(item => item.id !== 'overview' && item.id !== 'access').map(item => {
                      const Icon = item.icon;
                      return (
                        <button
                          key={item.id}
                          type="button"
                          disabled={!selectedProjectId}
                          onClick={() => onProjectManageSectionChange(item.id)}
                          className="flex min-h-[78px] items-start gap-2 rounded-md border border-neutral-800 bg-[#0d1218] p-3 text-left text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 disabled:cursor-not-allowed disabled:opacity-40"
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
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-neutral-800 bg-[#111820] text-neutral-300">
                    <ActiveProjectManageIcon className="h-4 w-4" />
                  </span>
                  <div className="min-w-0">
                    <h2 className="text-sm font-semibold text-neutral-100">{activeProjectManageItem.label}</h2>
                    <p className="mt-1 text-xs text-neutral-500">{activeProjectManageItem.description}</p>
                  </div>
                </div>
                {activeProjectSettingsSection && (
                  <SettingsSectionRouter {...settingsRouterProps} activeSection={activeProjectSettingsSection} />
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
  onDirectionClick: (directionId: string) => void;
  onConfigureTargets: () => void;
  onSpawnOrphanFix: DashboardHomeProps['onSpawnOrphanFix'];
  onPanelReviewPr: DashboardHomeProps['onPanelReviewPr'];
  onApproveExperiment: (taskId: string) => Promise<void>;
  onDismissExperiment: (taskId: string) => Promise<void>;
  onNavigateToKnowledge: (documentId: string) => void;
  onRestartOrchestrator: () => Promise<void>;
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
  onDirectionClick,
  onConfigureTargets,
  onSpawnOrphanFix,
  onPanelReviewPr,
  onApproveExperiment,
  onDismissExperiment,
  onNavigateToKnowledge,
  onRestartOrchestrator,
}: RuntimeWorkspaceProps) {
  const runtimeSettingsSections = RUNTIME_SETTINGS_SECTIONS.filter(item => !item.adminOnly || userRole === 'admin');
  const activeRuntimeSettingsItem =
    runtimeSettingsSections.find(item => item.id === runtimeConfigSection) ?? runtimeSettingsSections[0]!;
  const ActiveRuntimeSettingsIcon = activeRuntimeSettingsItem.icon;
  const actionButtonClass = 'inline-flex min-h-9 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-3 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100';

  const runtimeSurfaceActions = (
    <>
      <span className="hidden min-h-9 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-2.5 text-xs text-neutral-500 md:inline-flex">
        <span className={`h-1.5 w-1.5 rounded-full ${apiHealthy && wsConnected ? 'bg-emerald-400' : 'bg-amber-400'}`} />
        {apiHealthy && wsConnected ? 'runtime healthy' : 'runtime degraded'}
      </span>
      {userRole === 'admin' && (
        <button
          type="button"
          onClick={() => { void onRestartOrchestrator(); }}
          className={actionButtonClass}
        >
          <RotateCw className="h-3.5 w-3.5" />
          Restart orchestrator
        </button>
      )}
    </>
  );

  return (
    <WorkbenchSurface
      title="Runtime"
      subtitle={runtimeSection === 'devices' && selectedProjectRepoLabel ? selectedProjectRepoLabel : 'Global operations, accounts, and fleet health'}
      tabs={[
        { id: 'health', label: 'Health' },
        { id: 'devices', label: 'Devices' },
        { id: 'compute', label: 'Compute' },
        { id: 'accounts', label: 'Accounts' },
        { id: 'settings', label: 'Settings' },
        { id: 'analytics', label: 'Analytics' },
      ]}
      activeTab={runtimeSection}
      onTabChange={onRuntimeSectionChange}
      actions={runtimeSurfaceActions}
      tabLayout="stacked"
      flush
      contentClassName={runtimeSection === 'settings' ? 'min-h-0 min-w-0 overflow-hidden' : 'min-h-0 min-w-0 overflow-auto p-4 custom-scrollbar'}
    >
      {runtimeSection === 'health' ? (
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
      ) : runtimeSection === 'devices' ? (
        <div className="grid min-h-full gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,420px)]">
          <div className="min-w-0 rounded-md border border-neutral-800 bg-[#111820] p-4">
            <WarmVmPanel onViewTask={onViewTaskById} projects={projects} />
          </div>
          <WorkbenchPanel title="Self-hosted runners" meta={selectedProjectRepoLabel ?? 'select project'}>
            {selectedProjectId ? (
              <FleetRunnersPanel projectId={selectedProjectId} />
            ) : (
              <div className="py-10 text-center text-xs text-neutral-500">
                Select a project to manage self-hosted runners.
              </div>
            )}
          </WorkbenchPanel>
        </div>
      ) : runtimeSection === 'compute' ? (
        <div className="mx-auto max-w-7xl">
          <div className="mb-3 flex min-w-0 items-center gap-3 border-b border-neutral-800 pb-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-neutral-800 bg-[#111820] text-neutral-300">
              <Cpu className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-neutral-100">Compute jobs</h2>
              <p className="mt-1 text-xs text-neutral-500">Active experiments, held approvals, and terminal run analysis.</p>
            </div>
          </div>
          <ComputeJobsPanel
            projectId={selectedProjectId}
            onNavigateToTask={onViewTaskById}
            onLaunchCompute={onLaunchCompute}
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
          userRole={userRole}
        />
      ) : runtimeSection === 'accounts' ? (
        <div className="mx-auto max-w-6xl">
          <SettingsSectionRouter {...settingsRouterProps} activeSection="accounts" />
        </div>
      ) : (
        <div className="grid h-full min-h-0 grid-cols-1 overflow-hidden lg:grid-cols-[280px_minmax(0,1fr)]">
          <aside className="min-h-0 overflow-auto border-b border-neutral-800 bg-[#0b0f14] p-3 custom-scrollbar lg:border-b-0 lg:border-r">
            <nav className="space-y-1" aria-label="Runtime settings sections">
              {runtimeSettingsSections.map(item => {
                const Icon = item.icon;
                const selected = item.id === activeRuntimeSettingsItem.id;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => onRuntimeConfigSectionChange(item.id)}
                    className={`flex w-full items-start gap-2 rounded-md border px-2.5 py-2 text-left transition-colors ${
                      selected
                        ? 'border-neutral-700 bg-neutral-800 text-neutral-100'
                        : 'border-transparent text-neutral-400 hover:border-neutral-800 hover:bg-neutral-900/70 hover:text-neutral-200'
                    }`}
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
            <div className="mx-auto max-w-6xl space-y-4">
              <div className="flex min-w-0 items-start gap-3 border-b border-neutral-800 pb-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-neutral-800 bg-[#111820] text-neutral-300">
                  <ActiveRuntimeSettingsIcon className="h-4 w-4" />
                </span>
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-neutral-100">{activeRuntimeSettingsItem.label}</h2>
                  <p className="mt-1 text-xs text-neutral-500">{activeRuntimeSettingsItem.description}</p>
                </div>
              </div>
              <SettingsSectionRouter {...settingsRouterProps} activeSection={activeRuntimeSettingsItem.id} />
            </div>
          </div>
        </div>
      )}
    </WorkbenchSurface>
  );
}
