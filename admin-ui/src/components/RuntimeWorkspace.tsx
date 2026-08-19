import { lazy, Suspense, useState, type ComponentProps, type ReactNode } from 'react';
import { RotateCw } from 'lucide-react';
const ComputeJobsPanel = lazy(() => import('./ComputeJobsPanel').then(m => ({ default: m.ComputeJobsPanel })));
const DashboardHome = lazy(() => import('./DashboardHome').then(m => ({ default: m.DashboardHome })));
import { ConsoleHome } from './ConsoleHome';
import { CompletionDebtPanel } from './CompletionDebtPanel';
const FleetRunnersPanel = lazy(() => import('./FleetRunnersPanel').then(m => ({ default: m.FleetRunnersPanel })));
import { RuntimeAccountsPanel } from './RuntimeAccountsPanel';
import type { SettingsSectionRouterProps } from './SettingsSectionRouter';
const SettingsSectionRouter = lazy(() => import('./SettingsSectionRouter').then(m => ({ default: m.SettingsSectionRouter })));
import type { Task } from './TaskCard';
const WarmVmPanel = lazy(() => import('./WarmVmPanel').then(m => ({ default: m.WarmVmPanel })));
const VersionLifecyclePanel = lazy(() => import('./VersionLifecyclePanel').then(m => ({ default: m.VersionLifecyclePanel })));
import { WorkbenchPanel, WorkbenchSurface } from './WorkbenchLayout';
import type { FailingTaskTypeEntry } from '../types/diagnostics';
import { fetchAgentServingShas, type AgentServingShasResponse, type VersionLifecycleResponse } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { hasVersionLifecycleData } from '../hooks/useVersionLifecycle';
import {
  RUNTIME_SECTIONS,
  resolveRuntimeItem,
  resolveRuntimeSettingsItem,
  visibleRuntimeSettingsSections,
  type RuntimeSection,
  type RuntimeSettingsSection,
  type SectionNavItem,
} from './OperatorWorkspaceSections';
import type { ComputeExperimentForm } from '../hooks/useTaskModal';
import type { Project } from '../types/project';
import { resolveAccess } from '../accessControl';
import { useProjectResourceSummary } from '../hooks/useProjectResourceSummary';
import { ProjectResourceAvailabilityCard } from './ProjectResourceAvailabilityCard';

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
  const selectedProjectRole = projects.find(project => project.id === selectedProjectId)?.my_role;
  const access = resolveAccess(userRole, selectedProjectRole);
  const isPlatformAdmin = access.isPlatformAdmin;
  const canViewProject = access.canViewProject;
  const canOperateProject = access.canOperateProject;
  const canManageProject = access.canManageProject;
  const runtimeSettingsSections = isPlatformAdmin ? visibleRuntimeSettingsSections(userRole) : [];
  const activeRuntimeSettingsItem = resolveRuntimeSettingsItem(runtimeConfigSection, userRole);
  const hasVersionData = hasVersionLifecycleData(versionLifecycle);
  const visibleRuntimeSections = RUNTIME_SECTIONS.filter(
    s => (isPlatformAdmin || s.id === 'devices' || s.id === 'compute' || s.id === 'version-lifecycle')
      && (s.id !== 'version-lifecycle' || hasVersionData),
  );
  const effectiveRuntimeSection = visibleRuntimeSections.some(item => item.id === runtimeSection)
    ? runtimeSection
    : visibleRuntimeSections[0]?.id ?? 'devices';
  const effectiveSelectedProjectId = canViewProject ? selectedProjectId : null;
  const resourceSummary = useProjectResourceSummary(
    effectiveSelectedProjectId,
    effectiveRuntimeSection === 'devices' || effectiveRuntimeSection === 'compute',
  );
  const [agentServingShas, setAgentServingShas] = useState<AgentServingShasResponse | null>(null);
  usePoller(async (signal) => {
    if (!selectedProjectId) { setAgentServingShas(null); return; }
    try {
      const data = await fetchAgentServingShas(selectedProjectId);
      if (!signal.aborted) setAgentServingShas(data);
    } catch {
      if (!signal.aborted) setAgentServingShas(null);
    }
  }, { intervalMs: DEFAULT_POLL_MS, enabled: !!selectedProjectId && canViewProject, deps: [selectedProjectId, canViewProject] });
  const activeRuntimeItem = resolveRuntimeItem(effectiveRuntimeSection);
  const actionButtonClass = 'inline-flex min-h-11 min-w-11 items-center justify-center gap-1.5 rounded-md border border-neutral-800 bg-workbench-shell/70 px-0 text-xs font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover hover:text-neutral-100 sm:min-h-9 sm:min-w-0 sm:px-3';
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
      activeTab={effectiveRuntimeSection}
      onTabChange={onRuntimeSectionChange}
      actions={runtimeSurfaceActions}
      tabLayout="stacked"
      flush
      contentClassName={effectiveRuntimeSection === 'settings' ? 'min-h-0 min-w-0 overflow-hidden' : 'min-h-0 min-w-0 overflow-auto p-3 custom-scrollbar'}
    >
      {effectiveRuntimeSection === 'health' ? (
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
              value={`${globalActiveCount} in progress / ${globalQueuedCount} queued`}
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
            isAdmin={userRole === 'admin'}
          />
          {selectedProjectId && (
            <CompletionDebtPanel
              projectId={selectedProjectId}
              addToast={addToast}
            />
          )}
        </div>
      ) : effectiveRuntimeSection === 'devices' ? (
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
          {effectiveSelectedProjectId && (
            <ProjectResourceAvailabilityCard {...resourceSummary} />
          )}
          <div className={`grid gap-3 ${isPlatformAdmin ? 'xl:grid-cols-[minmax(0,1fr)_minmax(360px,420px)]' : ''}`}>
            {isPlatformAdmin && (
              <section className="workbench-layer min-w-0 rounded-md border p-3" aria-label="Warm VM devices">
                <Suspense fallback={null}>
                  <WarmVmPanel onViewTask={onViewTaskById} projects={projects} />
                </Suspense>
              </section>
            )}
            <section className="workbench-layer min-w-0 rounded-md border p-3" aria-label="Self-hosted runner devices">
              {selectedProjectId && canViewProject ? (
                <Suspense fallback={null}>
                  <FleetRunnersPanel
                    projectId={selectedProjectId}
                    requireLocalRunner={settingsRouterProps.resourceEnvelopeData?.require_local_runner ?? false}
                    refreshSignal={fleetRunnersRefreshSignal}
                    live={wsConnected}
                    onNavigateToTask={onViewTaskById}
                    readOnly={!canManageProject}
                  />
                </Suspense>
              ) : (
                <div className="py-10 text-center text-xs text-neutral-500">
                  Select a project to manage self-hosted runners.
                </div>
              )}
            </section>
          </div>
        </div>
      ) : effectiveRuntimeSection === 'compute' ? (
        <div className="mx-auto flex max-w-7xl flex-col gap-3">
          <RuntimeSectionIntro
            item={activeRuntimeItem}
            title="Compute jobs"
          >
            <RuntimeStatusPill
              label="project"
              value={`${scopedActiveCount ?? 0} in progress / ${scopedQueuedCount ?? 0} queued`}
              tone={(scopedActiveCount ?? 0) > 0 ? 'info' : 'neutral'}
            />
            <RuntimeStatusPill
              label="fleet"
              value={`${globalActiveCount} in progress / ${globalQueuedCount} queued`}
              tone={globalActiveCount > 0 ? 'info' : 'neutral'}
            />
          </RuntimeSectionIntro>
          {effectiveSelectedProjectId && (
            <ProjectResourceAvailabilityCard {...resourceSummary} />
          )}
          <Suspense fallback={null}>
            <ComputeJobsPanel
              projectId={canViewProject ? selectedProjectId : null}
              onNavigateToTask={onViewTaskById}
              onLaunchCompute={onLaunchCompute}
              onCloneExperiment={onCloneExperiment}
              onApproveExperiment={onApproveExperiment}
              onDismissExperiment={onDismissExperiment}
              onNavigateToKnowledge={onNavigateToKnowledge}
              readOnly={!canOperateProject}
            />
          </Suspense>
        </div>
      ) : effectiveRuntimeSection === 'analytics' ? (
        <Suspense fallback={null}>
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
            gatedTaskTypes={gatedTaskTypes}
            userRole={userRole}
          />
        </Suspense>
      ) : effectiveRuntimeSection === 'version-lifecycle' ? (
        <div className="mx-auto max-w-6xl space-y-4">
          {selectedProjectId && canViewProject ? (
            <Suspense fallback={null}>
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
                readOnly={!canOperateProject}
                canManageProject={canManageProject}
                canManagePlatform={access.canManagePlatform}
              />
            </Suspense>
          ) : (
            <div className="py-10 text-center text-xs text-neutral-500">
              Select a project to view version lifecycle.
            </div>
          )}
        </div>
      ) : effectiveRuntimeSection === 'accounts' ? (
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
              <Suspense fallback={null}>
                <SettingsSectionRouter {...settingsRouterProps} hostKind="workspace" activeSection={activeRuntimeSettingsItem.id} />
              </Suspense>
            </div>
          </div>
        </div>
      )}
    </WorkbenchSurface>
  );
}
