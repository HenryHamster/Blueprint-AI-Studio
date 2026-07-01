import type { RefObject } from 'react';
import { useMemo, useState, useRef, useEffect } from 'react';
import { Activity, Microscope, BookOpen, LogOut, MoreHorizontal, Settings, Bell, Plus } from 'lucide-react';
import { AutonomousStatus } from './AutonomousStatus';
import { OrchestratorHealthBar } from './OrchestratorHealthBar';
import type { DetailedHealth, PipelineThrottle } from './OrchestratorHealthBar';
import { ProjectSwitcherDropdown } from './ProjectSwitcherDropdown';
import type { DeployState, OrchestratorHealth } from '../types/settings';
import type { Task } from '../types/task';
import type { Project } from '../types/project';
import type { PrimaryTab } from '../hooks/useUIState';

export interface HeaderBarProps {
  tasks: Task[];
  onRefresh: () => void;
  showResearchBrowser: boolean;
  onToggleResearchBrowser: () => void;
  showKnowledgeBrowser: boolean;
  onToggleKnowledgeBrowser: () => void;
  onOpenActivityFeed?: () => void;
  openMessageCount: number;
  newSinceLastVisitCount?: number;
  needsAttentionCount?: number;
  alertCount?: number;
  alertMaxSeverity?: 'ERROR' | 'WARNING' | 'INFO' | null;
  showAlertsPopover?: boolean;
  onToggleAlertsPopover?: () => void;
  onOpenNewTaskModal: () => void;
  onOpenSettings: () => void;
  onSignOut: () => void;
  onNewProject?: () => void;
  userEmail: string;
  wsRef: RefObject<WebSocket | null>;
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
  deployState: DeployState | null;
  selectedProjectId: string | null;
  projects: Project[];
  onSelectProject: (id: string | null) => void;
  activeTab: PrimaryTab;
  onTabChange: (tab: PrimaryTab) => void;
  onOpenCommandPalette?: () => void;
  health: OrchestratorHealth | null;
  detailedHealth: DetailedHealth | null;
  pipelineThrottle?: PipelineThrottle | null;
  costSectionAvailable?: boolean;
  /** Opens the ProjectManagementModal (archive/delete/unarchive + new-project). */
  onManageProjects?: () => void;
}

export interface PrimaryTabNavProps {
  activeTab: PrimaryTab;
  onTabChange: (tab: PrimaryTab) => void;
  openMessageCount?: number;
  newSinceLastVisitCount?: number;
  needsAttentionCount?: number;
  className?: string;
  placement?: 'header' | 'mobile';
}

function tabLabel(tab: PrimaryTab): string {
  return tab === 'chat' ? 'Chat' : tab.charAt(0).toUpperCase() + tab.slice(1);
}

export function PrimaryTabNav({
  activeTab,
  onTabChange,
  openMessageCount = 0,
  newSinceLastVisitCount = 0,
  needsAttentionCount = 0,
  className = '',
  placement = 'header',
}: PrimaryTabNavProps) {
  const mobile = placement === 'mobile';
  const navClassName = mobile
    ? `grid grid-cols-4 gap-1 border-t border-neutral-800/90 bg-[#020407] px-1.5 py-1.5 ${className}`
    : `hidden min-w-0 flex-1 items-center gap-0.5 overflow-x-auto rounded-md border border-neutral-800/80 bg-[#05080d] p-0.5 md:flex md:flex-none ${className}`;

  return (
    <nav
      data-viewport-allow-overflow
      data-viewport-allow-truncation
      data-testid={mobile ? 'mobile-primary-tab-nav' : 'primary-tab-nav'}
      className={navClassName}
      aria-label="Primary workbench sections"
    >
      {(['chat', 'pipeline', 'project', 'runtime'] as const).map(tab => {
        const isActive = activeTab === tab;
        const chatBadgeCount = tab === 'chat'
          ? Math.max(openMessageCount, newSinceLastVisitCount)
          : 0;
        const pipelineBadgeCount = tab === 'pipeline' ? needsAttentionCount : 0;
        const badgeCount = Math.max(chatBadgeCount, pipelineBadgeCount);
        const label = tabLabel(tab);
        return (
          <button
            key={tab}
            type="button"
            onClick={() => onTabChange(tab)}
            className={`inline-flex min-h-[44px] min-w-0 items-center justify-center rounded text-sm font-semibold leading-none transition-colors whitespace-nowrap ${
              mobile ? 'px-1.5' : 'shrink-0 px-2.5 py-1 md:min-h-6 md:px-2 md:text-xs'
            } ${
              isActive
                ? 'bg-neutral-800/90 text-neutral-100 shadow-[inset_0_0_0_1px_rgba(255,255,255,0.04)]'
                : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100'
            }`}
          >
            <span className="truncate">{label}</span>
            {badgeCount > 0 && (
              <span className="ml-1.5 inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full bg-emerald-400 px-1 text-ui-sm font-bold leading-none text-neutral-950">
                {badgeCount > 99 ? '99+' : badgeCount}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}


export function HeaderBar(props: HeaderBarProps) {
  const {
    tasks,
    onRefresh,
    showResearchBrowser,
    onToggleResearchBrowser,
    showKnowledgeBrowser,
    onToggleKnowledgeBrowser,
    onOpenActivityFeed,
    onOpenNewTaskModal,
    onOpenSettings,
    openMessageCount,
    newSinceLastVisitCount,
    wsRef,
    apiHealthy,
    wsConnected,
    lastFetchTime,
  } = props;
  const [showMobileMenu, setShowMobileMenu] = useState(false);
  const [showMobileShortcuts, setShowMobileShortcuts] = useState(false);
  const [healthExpanded, setHealthExpanded] = useState(false);
  const mobileMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!showMobileMenu) return;
    function handleClickOutside(e: MouseEvent) {
      if (mobileMenuRef.current && !mobileMenuRef.current.contains(e.target as Node)) {
        setShowMobileMenu(false);
        setShowMobileShortcuts(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showMobileMenu]);

  const pendingFindingsCount = useMemo(() => {
    // Build set of implemented finding keys
    const implemented = new Set<string>();
    for (const t of tasks) {
      const ctx = t.context as Record<string, unknown> | undefined;
      if (ctx?.research_task_id && ctx?.finding_index !== undefined) {
        implemented.add(`${ctx.research_task_id}-${ctx.finding_index}`);
      }
    }
    // Count unimplemented findings from completed research tasks
    let count = 0;
    for (const t of tasks) {
      if (t.task_type !== 'research' || t.status !== 'COMPLETED' || !t.research_findings) continue;
      const arr = Array.isArray(t.research_findings) ? t.research_findings : [];
      arr.forEach((_: unknown, i: number) => {
        if (!implemented.has(`${t.id}-${i}`)) count++;
      });
    }
    return count;
  }, [tasks]);

  const healthDotColor = useMemo(() => {
    if (!apiHealthy) return 'bg-red-500 animate-pulse';
    if (!props.health) return 'bg-neutral-500';
    if (props.health.orchestrator_hung) return 'bg-red-500 animate-pulse';
    const cycleAge = props.health.last_cycle_seconds_ago;
    if (cycleAge !== null && cycleAge >= 120) return 'bg-red-500 animate-pulse';
    if (cycleAge !== null && cycleAge >= 60) return 'bg-amber-400';
    return 'bg-emerald-500';
  }, [apiHealthy, props.health]);
  return (
    <div className="shrink-0">
      {/* Health expanded panel — shows full OrchestratorHealthBar below the top bar on click */}
      {healthExpanded && (
        <div className="mb-1">
          <OrchestratorHealthBar
            health={props.health}
            detailedHealth={props.detailedHealth}
            apiHealthy={apiHealthy}
            wsConnected={wsConnected}
            lastFetchTime={lastFetchTime}
            tasks={tasks}
            pipelineThrottle={props.pipelineThrottle}
            costSectionAvailable={props.costSectionAvailable}
          />
        </div>
      )}

      {/* Unified top bar: compact global shell with fixed utility clusters and a scrolling tab strip. */}
      <header className="flex min-h-10 flex-wrap items-center gap-1 border-b border-neutral-800/90 bg-[#020407] px-1.5 py-0.5 md:min-h-8 md:flex-nowrap md:px-2">
        {/* LEFT: ProjectSwitcher + Manage projects slot */}
        <div className="flex min-w-0 shrink-0 items-center gap-1">
          <div className="hidden items-center lg:flex" aria-label="Blueprint">
            <span className="grid h-4 w-4 grid-cols-2 gap-0.5 rounded border border-neutral-800/80 bg-[#05080d] p-0.5" aria-hidden="true">
              <span className="rounded-[1px] bg-neutral-500/80" />
              <span className="rounded-[1px] bg-neutral-700" />
              <span className="rounded-[1px] bg-neutral-700" />
              <span className="rounded-[1px] bg-emerald-400/80" />
            </span>
          </div>
          <ProjectSwitcherDropdown
            projects={props.projects}
            selectedProjectId={props.selectedProjectId}
            onSelectProject={props.onSelectProject}
          />
          {props.onManageProjects && (
            /* eslint-disable-next-line viewport/min-touch-target */
            <button
              onClick={props.onManageProjects}
              className="hidden rounded px-1.5 py-1 text-xs text-neutral-600 transition-colors hover:bg-neutral-900 hover:text-neutral-300 sm:inline-flex"
              title="Manage projects"
            >
              Manage
            </button>
          )}
        </div>

        {/* CENTER: Primary tab group — intentional horizontal-scroll strip
            (overflow-x-auto + min-w-0) so the tabs scroll rather than wrap or
            push the bar wide on narrow viewports; opt the scroll container out
            of the overflow/truncation invariants (its descendants legitimately
            extend past the fold inside the scroll region). */}
        <PrimaryTabNav
          activeTab={props.activeTab}
          onTabChange={props.onTabChange}
          openMessageCount={openMessageCount}
          newSinceLastVisitCount={newSinceLastVisitCount}
          needsAttentionCount={props.needsAttentionCount}
        />

        {/* RIGHT: Icon cluster + mobile controls + Launch Task */}
        <div className="ml-auto flex shrink-0 items-center gap-0.5">

          {/* Desktop icon cluster */}
          <div className="hidden items-center gap-0.5 rounded-md border border-neutral-800/80 bg-[#05080d] p-0.5 md:flex">
            {/* Alerts — only when active alerts exist */}
            {(props.alertCount ?? 0) > 0 && (
              /* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */
              <button
                onClick={props.onToggleAlertsPopover}
                title={`${props.alertCount} alert${(props.alertCount ?? 0) > 1 ? 's' : ''}`}
                className={`flex h-7 items-center gap-1 rounded px-1.5 text-xs transition-colors ${
                  props.showAlertsPopover
                    ? (props.alertMaxSeverity === 'ERROR' ? 'text-red-400 bg-red-500/10'
                      : props.alertMaxSeverity === 'WARNING' ? 'text-amber-400 bg-amber-500/10'
                      : 'text-blue-400 bg-blue-500/10')
                    : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
                }`}
              >
                <span className={`w-2 h-2 rounded-full ${
                  props.alertMaxSeverity === 'ERROR' ? 'bg-red-500' :
                  props.alertMaxSeverity === 'WARNING' ? 'bg-amber-500' : 'bg-blue-500'
                }`} />
                <Bell className="w-3.5 h-3.5" />
                <span>{props.alertCount}</span>
              </button>
            )}
            <button
              onClick={onToggleResearchBrowser}
              title="Research findings"
              aria-pressed={showResearchBrowser}
              className={`relative flex h-7 w-7 items-center justify-center rounded text-xs transition-colors ${
                showResearchBrowser ? 'bg-cyan-500/10 text-cyan-300' : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200'
              }`}
            >
              <Microscope className="h-3.5 w-3.5" />
              {pendingFindingsCount > 0 && (
                <span className="absolute -right-0.5 -top-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-cyan-400 px-0.5 text-ui-2xs font-bold leading-none text-neutral-950">
                  {pendingFindingsCount > 9 ? '9+' : pendingFindingsCount}
                </span>
              )}
            </button>
            <button
              onClick={onToggleKnowledgeBrowser}
              title="Knowledge library"
              aria-pressed={showKnowledgeBrowser}
              className={`flex h-7 w-7 items-center justify-center rounded text-xs transition-colors ${
                showKnowledgeBrowser ? 'bg-violet-500/10 text-violet-300' : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200'
              }`}
            >
              <BookOpen className="h-3.5 w-3.5" />
            </button>
            {props.activeTab !== 'pipeline' && (
              /* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */
              <button
                onClick={onOpenNewTaskModal}
                title="New task"
                className="flex h-7 items-center gap-1 rounded px-1.5 text-xs font-semibold text-neutral-300 transition-colors hover:bg-neutral-900 hover:text-neutral-100"
              >
                <Plus className="h-3.5 w-3.5" />
                <span>New</span>
              </button>
            )}
            {/* Activity */}
            {onOpenActivityFeed && (
              <button
                onClick={onOpenActivityFeed}
                title="Chronological activity stream"
                className="flex h-7 w-7 items-center justify-center rounded text-xs text-neutral-400 transition-colors hover:bg-neutral-900 hover:text-neutral-200"
              >
                <Activity className="h-3.5 w-3.5" />
              </button>
            )}
            {/* Health pill — expands/collapses the full OrchestratorHealthBar */}
            {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
            <button
              onClick={() => setHealthExpanded(prev => !prev)}
              title={healthExpanded ? 'Hide health panel' : 'Show orchestrator health'}
              className={`flex h-7 items-center gap-1 rounded px-1.5 text-xs transition-colors ${healthExpanded ? 'bg-neutral-800/90 text-neutral-200' : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200'}`}
            >
              <span className={`w-2 h-2 rounded-full shrink-0 ${healthDotColor}`} />
              <span className="hidden font-mono xl:inline">health</span>
            </button>
          </div>

          <button
            onClick={onOpenSettings}
            title="Runtime settings"
            className="hidden h-7 w-7 items-center justify-center rounded border border-neutral-800/80 bg-[#070a0f] text-neutral-400 transition-colors hover:bg-neutral-900 hover:text-neutral-100 md:inline-flex"
          >
            <Settings className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={props.onSignOut}
            title={props.userEmail ? `Sign out ${props.userEmail}` : 'Sign out'}
            className="hidden h-7 w-7 items-center justify-center rounded border border-neutral-800/80 bg-[#070a0f] text-neutral-500 transition-colors hover:bg-red-950/20 hover:text-red-300 md:inline-flex"
          >
            <LogOut className="h-3.5 w-3.5" />
          </button>

          {/* Mobile-only autonomy control */}
          <div className="md:hidden flex items-center mr-1">
            <AutonomousStatus
              onRefreshTasks={onRefresh}
              wsRef={wsRef}
              pauseReason={props.deployState?.reason ?? null}
            />
          </div>

          {/* Mobile overflow menu — hidden on desktop */}
          <div className="relative md:hidden" ref={mobileMenuRef}>
            <button
              onClick={() => setShowMobileMenu(prev => !prev)}
              className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md border border-neutral-800 bg-[#0b0f14] p-3 text-xs text-neutral-300 transition-colors hover:bg-neutral-900"
              title="More options"
            >
              <MoreHorizontal className="w-4 h-4" />
            </button>
            {showMobileMenu && (
              <div className="absolute right-0 top-full z-50 mt-2 max-h-[calc(100vh-96px)] w-60 overflow-auto rounded-md border border-neutral-800 bg-[#0b0f14] p-2 shadow-2xl">
                <button
                  onClick={() => { onToggleResearchBrowser(); setShowMobileMenu(false); }}
                  className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${showResearchBrowser ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
                >
                  <Microscope className="w-4 h-4 shrink-0" />
                  <span>Research Browser</span>
                  {pendingFindingsCount > 0 && (
                    <span className="ml-auto bg-cyan-500 text-black text-ui-xs font-bold px-1.5 py-0.5 rounded-full leading-none">
                      {pendingFindingsCount}
                    </span>
                  )}
                </button>
                <button
                  onClick={() => { onToggleKnowledgeBrowser(); setShowMobileMenu(false); }}
                  className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${showKnowledgeBrowser ? 'text-violet-400 bg-violet-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
                >
                  <BookOpen className="w-4 h-4 shrink-0" />
                  <span>Knowledge Library</span>
                </button>
                {onOpenActivityFeed && (
                  <button
                    onClick={() => { onOpenActivityFeed(); setShowMobileMenu(false); }}
                    className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
                  >
                    <Activity className="w-4 h-4 shrink-0" />
                    <span>Activity stream</span>
                  </button>
                )}
                <button
                  onClick={() => {
                    setHealthExpanded(prev => !prev);
                    setShowMobileMenu(false);
                    setShowMobileShortcuts(false);
                  }}
                  className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${
                    healthExpanded ? 'bg-neutral-800/90 text-neutral-100' : 'text-neutral-300 hover:bg-neutral-800'
                  }`}
                >
                  <span className={`h-2 w-2 rounded-full shrink-0 ${healthDotColor}`} />
                  <span>{healthExpanded ? 'Hide health panel' : 'Health panel'}</span>
                </button>
                {props.activeTab !== 'pipeline' && (
                  <button
                    onClick={() => { onOpenNewTaskModal(); setShowMobileMenu(false); }}
                    className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
                  >
                    <span className="w-4 h-4 flex items-center justify-center font-mono shrink-0">N</span>
                    <span>New task</span>
                  </button>
                )}
                <button
                  onClick={() => setShowMobileShortcuts(prev => !prev)}
                  className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
                >
                  <span className="w-4 h-4 flex items-center justify-center font-mono shrink-0">?</span>
                  <span>Keyboard Shortcuts</span>
                  <span className="ml-auto text-neutral-500 text-xs">{showMobileShortcuts ? '▴' : '▾'}</span>
                </button>
                {showMobileShortcuts && (
                  <div className="mx-2 mb-1 bg-neutral-800/50 rounded p-2">
                    <div className="space-y-1.5 text-xs">
                      {[
                        ['Ctrl+K', 'Command palette'],
                        ['N', 'New task'],
                        ['R', 'Refresh'],
                        ['1', 'Chat'],
                        ['2', 'Pipeline'],
                        ['3', 'Project update'],
                        ['4', 'Runtime'],
                        ['S', 'Runtime settings'],
                        ['A', 'Archive (pipeline)'],
                        ['Esc', 'Close panel/modal'],
                        ['Del', 'Cancel selected (selection mode)'],
                        ['R*', 'Retry selected (selection mode)'],
                        ['Enter', 'Open selected task (selection mode)'],
                        ['Esc*', 'Clear selection / exit mode'],
                      ].map(([key, desc]) => (
                        <div key={key} className="flex items-center justify-between">
                          <span className="text-neutral-400">{desc}</span>
                          <kbd className="bg-neutral-800 border border-neutral-700 text-neutral-300 px-1.5 py-0.5 rounded text-ui-sm font-mono min-w-[24px] text-center">{key}</kbd>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <button
                  onClick={() => { onOpenSettings(); setShowMobileMenu(false); }}
                  className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
                >
                  <Settings className="w-4 h-4 shrink-0" />
                  <span>Runtime settings</span>
                </button>
                <button
                  onClick={() => { props.onSignOut(); setShowMobileMenu(false); }}
                  className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-400 hover:bg-red-950/20 hover:text-red-300"
                >
                  <LogOut className="w-4 h-4 shrink-0" />
                  <span>Sign out</span>
                </button>
              </div>
            )}
          </div>

        </div>
      </header>
    </div>
  );
}
