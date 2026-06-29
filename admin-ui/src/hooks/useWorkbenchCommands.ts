import { useMemo } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import type { ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import type { StrategicInteractionMode } from '../components/StrategicSessionControl';
import type { SectionId } from '../components/SettingsDropdown';
import type { PrimaryTab } from './useUIState';
import type { ViewMode } from './useViewPreferences';

export type PipelineSection = 'flow' | 'board' | 'archive';

interface UseWorkbenchCommandsOptions {
  selectedProjectId: string | null;
  selectedProjectRepoLabel: string | null;
  directionsCount: number;
  filteredTaskCount: number;
  visibleActiveCount: number;
  visibleQueuedCount: number;
  maxPlanAccountCount: number;
  apiHealthy: boolean;
  problemModelRevision?: number | null;
  hasDesignDoc?: boolean;
  designRevision?: number | null;
  strategicInteractionMode: StrategicInteractionMode;
  savingStrategicWorkbench?: boolean;
  switchStrategicInteractionMode: (mode: StrategicInteractionMode) => void;
  focusStrategicComposer: () => void;
  handleTabChange: (tab: PrimaryTab) => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setPipelineSection: (section: PipelineSection) => void;
  setProjectSection: (section: ProjectSection) => void;
  setRuntimeSection: (section: RuntimeSection) => void;
  setRuntimeConfigSection: (section: RuntimeSettingsSection) => void;
  setViewMode: (mode: ViewMode) => void;
  setShowArchive: (show: boolean) => void;
  setShowActivityFeed: (show: boolean) => void;
  setShowResearchBrowser: (show: boolean) => void;
  setShowKnowledgeBrowser: (show: boolean) => void;
  openNewTaskModal: () => void;
  openGlobalSettings: (section: SectionId | null) => void;
  refreshWorkbench: () => void;
  openProjectManagement: () => void;
  openProjectMembers: () => void;
}

export function useWorkbenchCommands({
  selectedProjectId,
  selectedProjectRepoLabel,
  directionsCount,
  filteredTaskCount,
  visibleActiveCount,
  visibleQueuedCount,
  maxPlanAccountCount,
  apiHealthy,
  problemModelRevision,
  hasDesignDoc,
  designRevision,
  strategicInteractionMode,
  savingStrategicWorkbench = false,
  switchStrategicInteractionMode,
  focusStrategicComposer,
  handleTabChange,
  setCommandPaletteOpen,
  setPipelineSection,
  setProjectSection,
  setRuntimeSection,
  setRuntimeConfigSection,
  setViewMode,
  setShowArchive,
  setShowActivityFeed,
  setShowResearchBrowser,
  setShowKnowledgeBrowser,
  openNewTaskModal,
  openGlobalSettings,
  refreshWorkbench,
  openProjectManagement,
  openProjectMembers,
}: UseWorkbenchCommandsOptions): WorkbenchCommand[] {
  return useMemo(() => {
    const projectDisabled = !selectedProjectId;
    const closePalette = () => setCommandPaletteOpen(false);
    const runAndClose = (run: () => void) => {
      run();
      closePalette();
    };
    const openProjectSection = (section: ProjectSection) => {
      setProjectSection(section);
      handleTabChange('project');
      closePalette();
    };
    const openRuntimeSection = (section: RuntimeSection) => {
      setRuntimeSection(section);
      handleTabChange('runtime');
      closePalette();
    };
    const openPipelineSection = (section: PipelineSection) => {
      setPipelineSection(section);
      if (section === 'board') setViewMode('kanban');
      if (section === 'flow') setViewMode('pipeline');
      setShowArchive(section === 'archive');
      handleTabChange('pipeline');
      closePalette();
    };
    const setStrategicModeAndOpenChat = (mode: StrategicInteractionMode) => {
      switchStrategicInteractionMode(mode);
      closePalette();
    };
    const strategicModeCommands: Array<{
      id: string;
      mode: StrategicInteractionMode;
      label: string;
      detail: string;
      keywords: string[];
    }> = [
      {
        id: 'strategic.mode.chat',
        mode: 'chat',
        label: 'Switch Strategic to Chat',
        detail: 'Conversation-first steering',
        keywords: ['chat', 'conversation'],
      },
      {
        id: 'strategic.mode.check_in',
        mode: 'check_in',
        label: 'Switch Strategic to Check-in',
        detail: 'Ask for a concise status pass',
        keywords: ['check in', 'check-in', 'status'],
      },
      {
        id: 'strategic.mode.monitor',
        mode: 'monitor',
        label: 'Switch Strategic to Monitor',
        detail: 'Watch work without waking Strategic',
        keywords: ['monitor', 'passive'],
      },
      {
        id: 'strategic.mode.continuous',
        mode: 'continuous',
        label: 'Switch Strategic to Workday',
        detail: 'Keep Strategic ready for active work',
        keywords: ['workday', 'continuous', 'active'],
      },
    ];

    return [
      {
        id: 'strategic.focus',
        label: 'Strategic chat',
        section: 'Strategic',
        detail: selectedProjectRepoLabel ?? 'No project selected',
        keywords: ['chat', 'message', 'steer', 'strategist'],
        shortcut: '1',
        disabled: projectDisabled,
        run: focusStrategicComposer,
      },
      ...strategicModeCommands.map((command): WorkbenchCommand => ({
        id: command.id,
        label: command.label,
        section: 'Strategic',
        detail: command.mode === strategicInteractionMode ? 'Current mode' : command.detail,
        keywords: ['mode', 'session', 'interaction', 'strategic', ...command.keywords],
        disabled: projectDisabled || savingStrategicWorkbench || command.mode === strategicInteractionMode,
        run: command.mode === strategicInteractionMode ? closePalette : () => setStrategicModeAndOpenChat(command.mode),
      })),
      {
        id: 'workbench.refresh',
        label: 'Refresh',
        section: 'Workbench',
        detail: 'Tasks + Strategic',
        keywords: ['reload', 'sync', 'status'],
        shortcut: 'R',
        run: () => runAndClose(refreshWorkbench),
      },
      {
        id: 'pipeline.flow',
        label: 'Pipeline',
        section: 'Pipeline',
        detail: `${visibleActiveCount} active, ${visibleQueuedCount} queued`,
        keywords: ['tasks', 'flow', 'monitor'],
        shortcut: '2',
        run: () => openPipelineSection('flow'),
      },
      {
        id: 'pipeline.board',
        label: 'Task board',
        section: 'Pipeline',
        detail: `${filteredTaskCount} visible tasks`,
        keywords: ['kanban', 'tasks', 'board'],
        run: () => openPipelineSection('board'),
      },
      {
        id: 'pipeline.archive',
        label: 'Archive',
        section: 'Pipeline',
        detail: 'Terminal work',
        keywords: ['archived', 'terminal'],
        shortcut: 'A',
        run: () => openPipelineSection('archive'),
      },
      {
        id: 'project.update',
        label: 'Project update',
        section: 'Project',
        detail: selectedProjectRepoLabel ?? 'No project selected',
        keywords: ['status', 'context', 'brief'],
        shortcut: '3',
        disabled: projectDisabled,
        run: () => openProjectSection('running-update'),
      },
      {
        id: 'project.directions',
        label: 'Directions',
        section: 'Project',
        detail: `${directionsCount} loaded`,
        keywords: ['strategy', 'plans'],
        disabled: projectDisabled,
        run: () => openProjectSection('directions'),
      },
      {
        id: 'project.model',
        label: 'Problem model',
        section: 'Project',
        detail: problemModelRevision ? `rev ${problemModelRevision}` : 'No revision',
        keywords: ['problem', 'model', 'gaps'],
        disabled: projectDisabled,
        run: () => openProjectSection('problem-model'),
      },
      {
        id: 'project.design',
        label: 'Design doc',
        section: 'Project',
        detail: hasDesignDoc ? `rev ${designRevision ?? 'loaded'}` : 'No design doc',
        keywords: ['architecture', 'design'],
        disabled: projectDisabled || !hasDesignDoc,
        run: () => openProjectSection('design-doc'),
      },
      {
        id: 'project.manage',
        label: 'Project settings',
        section: 'Project',
        detail: 'Repo, members, policy',
        keywords: ['settings', 'members', 'repo'],
        disabled: projectDisabled,
        run: () => openProjectSection('manage'),
      },
      {
        id: 'project.members',
        label: 'Project members',
        section: 'Project',
        detail: 'Access',
        keywords: ['access', 'team', 'users'],
        disabled: projectDisabled,
        run: () => runAndClose(openProjectMembers),
      },
      {
        id: 'project.switcher',
        label: 'Projects',
        section: 'Project',
        detail: 'Create, archive, restore',
        keywords: ['project management', 'archive', 'delete'],
        run: () => runAndClose(openProjectManagement),
      },
      {
        id: 'runtime.health',
        label: 'Runtime',
        section: 'Runtime',
        detail: apiHealthy ? 'API healthy' : 'API degraded',
        keywords: ['devices', 'runner', 'orchestrator'],
        shortcut: '4',
        run: () => openRuntimeSection('health'),
      },
      {
        id: 'runtime.devices',
        label: 'Devices',
        section: 'Runtime',
        detail: 'Runners',
        keywords: ['runner', 'local', 'hardware'],
        run: () => openRuntimeSection('devices'),
      },
      {
        id: 'runtime.accounts',
        label: 'Accounts',
        section: 'Runtime',
        detail: `${maxPlanAccountCount} accounts`,
        keywords: ['cost', 'spend', 'models'],
        run: () => openRuntimeSection('accounts'),
      },
      {
        id: 'runtime.compute',
        label: 'Compute jobs',
        section: 'Runtime',
        detail: 'Experiments',
        keywords: ['jobs', 'experiments'],
        run: () => openRuntimeSection('compute'),
      },
      {
        id: 'runtime.settings',
        label: 'Runtime settings',
        section: 'Runtime',
        detail: 'Orchestrator',
        keywords: ['orchestrator', 'provider', 'routing'],
        run: () => {
          setRuntimeSection('settings');
          setRuntimeConfigSection('orchestrator');
          handleTabChange('runtime');
          closePalette();
        },
      },
      {
        id: 'activity.open',
        label: 'Activity stream',
        section: 'Workbench',
        detail: 'Recent events',
        keywords: ['events', 'recent'],
        run: () => {
          setShowActivityFeed(true);
          closePalette();
        },
      },
      {
        id: 'research.open',
        label: 'Findings journal',
        section: 'Workbench',
        detail: 'Research outputs',
        keywords: ['research', 'findings'],
        disabled: projectDisabled,
        run: () => {
          setShowResearchBrowser(true);
          closePalette();
        },
      },
      {
        id: 'knowledge.open',
        label: 'Knowledge library',
        section: 'Workbench',
        detail: 'Project docs',
        keywords: ['documents', 'library'],
        disabled: projectDisabled,
        run: () => {
          setShowKnowledgeBrowser(true);
          closePalette();
        },
      },
      {
        id: 'task.new',
        label: 'New task',
        section: 'Pipeline',
        detail: selectedProjectRepoLabel ?? 'No project selected',
        keywords: ['new', 'launch'],
        shortcut: 'N',
        disabled: projectDisabled,
        run: () => {
          openNewTaskModal();
          closePalette();
        },
      },
      {
        id: 'settings.general',
        label: 'Settings',
        section: 'Workbench',
        detail: 'Defaults and controls',
        keywords: ['preferences', 'config'],
        shortcut: 'S',
        run: () => openGlobalSettings('general'),
      },
    ];
  }, [
    apiHealthy,
    designRevision,
    directionsCount,
    filteredTaskCount,
    focusStrategicComposer,
    handleTabChange,
    hasDesignDoc,
    maxPlanAccountCount,
    openGlobalSettings,
    openNewTaskModal,
    openProjectManagement,
    openProjectMembers,
    problemModelRevision,
    refreshWorkbench,
    savingStrategicWorkbench,
    selectedProjectId,
    selectedProjectRepoLabel,
    setCommandPaletteOpen,
    setPipelineSection,
    setProjectSection,
    setRuntimeConfigSection,
    setRuntimeSection,
    setShowActivityFeed,
    setShowArchive,
    setShowKnowledgeBrowser,
    setShowResearchBrowser,
    switchStrategicInteractionMode,
    setViewMode,
    strategicInteractionMode,
    visibleActiveCount,
    visibleQueuedCount,
  ]);
}
