import { useMemo } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import type { ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
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

    return [
      {
        id: 'strategic.focus',
        label: 'Talk to Strategic',
        section: 'Strategic',
        detail: selectedProjectRepoLabel ?? 'Open the Strategic conversation',
        keywords: ['chat', 'message', 'steer', 'strategist'],
        shortcut: 'Global 1',
        disabled: projectDisabled,
        run: focusStrategicComposer,
      },
      {
        id: 'workbench.refresh',
        label: 'Refresh workbench',
        section: 'Workbench',
        detail: 'Tasks and Strategic context',
        keywords: ['reload', 'sync', 'status'],
        shortcut: 'Global R',
        run: () => runAndClose(refreshWorkbench),
      },
      {
        id: 'pipeline.flow',
        label: 'Watch pipeline flow',
        section: 'Pipeline',
        detail: `${visibleActiveCount} active, ${visibleQueuedCount} queued`,
        keywords: ['tasks', 'flow', 'monitor'],
        shortcut: 'Global 2',
        run: () => openPipelineSection('flow'),
      },
      {
        id: 'pipeline.board',
        label: 'Open task board',
        section: 'Pipeline',
        detail: `${filteredTaskCount} visible tasks`,
        keywords: ['kanban', 'tasks', 'board'],
        run: () => openPipelineSection('board'),
      },
      {
        id: 'pipeline.archive',
        label: 'Open archive',
        section: 'Pipeline',
        detail: 'Terminal and archived work',
        keywords: ['archived', 'terminal'],
        shortcut: 'Global A',
        run: () => openPipelineSection('archive'),
      },
      {
        id: 'project.update',
        label: 'Open running update',
        section: 'Project',
        detail: selectedProjectRepoLabel ?? 'Project context',
        keywords: ['status', 'context', 'brief'],
        shortcut: 'Global 3',
        disabled: projectDisabled,
        run: () => openProjectSection('running-update'),
      },
      {
        id: 'project.directions',
        label: 'Open directions',
        section: 'Project',
        detail: `${directionsCount} loaded`,
        keywords: ['strategy', 'plans'],
        disabled: projectDisabled,
        run: () => openProjectSection('directions'),
      },
      {
        id: 'project.model',
        label: 'Open problem model',
        section: 'Project',
        detail: problemModelRevision ? `revision ${problemModelRevision}` : 'Project model',
        keywords: ['problem', 'model', 'gaps'],
        disabled: projectDisabled,
        run: () => openProjectSection('problem-model'),
      },
      {
        id: 'project.design',
        label: 'Open design doc',
        section: 'Project',
        detail: hasDesignDoc ? `revision ${designRevision ?? 'loaded'}` : 'No design doc loaded',
        keywords: ['architecture', 'design'],
        disabled: projectDisabled,
        run: () => openProjectSection('design-doc'),
      },
      {
        id: 'project.manage',
        label: 'Manage project',
        section: 'Project',
        detail: 'Repository, members, and policy',
        keywords: ['settings', 'members', 'repo'],
        disabled: projectDisabled,
        run: () => openProjectSection('manage'),
      },
      {
        id: 'project.members',
        label: 'Open project members',
        section: 'Project',
        detail: 'Access for the selected project',
        keywords: ['access', 'team', 'users'],
        disabled: projectDisabled,
        run: () => runAndClose(openProjectMembers),
      },
      {
        id: 'project.switcher',
        label: 'Manage projects',
        section: 'Project',
        detail: 'Create, archive, restore, or delete projects',
        keywords: ['project management', 'archive', 'delete'],
        run: () => runAndClose(openProjectManagement),
      },
      {
        id: 'runtime.health',
        label: 'Open runtime health',
        section: 'Runtime',
        detail: apiHealthy ? 'API healthy' : 'API degraded',
        keywords: ['devices', 'runner', 'orchestrator'],
        shortcut: 'Global 4',
        run: () => openRuntimeSection('health'),
      },
      {
        id: 'runtime.devices',
        label: 'Open connected devices',
        section: 'Runtime',
        detail: 'Fleet and runner registration',
        keywords: ['runner', 'local', 'hardware'],
        run: () => openRuntimeSection('devices'),
      },
      {
        id: 'runtime.accounts',
        label: 'Open accounts and usage',
        section: 'Runtime',
        detail: `${maxPlanAccountCount} accounts`,
        keywords: ['cost', 'spend', 'models'],
        run: () => openRuntimeSection('accounts'),
      },
      {
        id: 'runtime.compute',
        label: 'Open compute jobs',
        section: 'Runtime',
        detail: 'Experiments and analysis jobs',
        keywords: ['jobs', 'experiments'],
        run: () => openRuntimeSection('compute'),
      },
      {
        id: 'runtime.settings',
        label: 'Open runtime settings',
        section: 'Runtime',
        detail: 'Global automation controls',
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
        label: 'Open activity stream',
        section: 'Workbench',
        detail: 'Chronological operator feed',
        keywords: ['events', 'recent'],
        run: () => {
          setShowActivityFeed(true);
          closePalette();
        },
      },
      {
        id: 'research.open',
        label: 'Open findings journal',
        section: 'Workbench',
        detail: 'Research outputs and pending findings',
        keywords: ['research', 'findings'],
        disabled: projectDisabled,
        run: () => {
          setShowResearchBrowser(true);
          closePalette();
        },
      },
      {
        id: 'knowledge.open',
        label: 'Open knowledge library',
        section: 'Workbench',
        detail: 'Project knowledge documents',
        keywords: ['documents', 'library'],
        disabled: projectDisabled,
        run: () => {
          setShowKnowledgeBrowser(true);
          closePalette();
        },
      },
      {
        id: 'task.new',
        label: 'Create task',
        section: 'Pipeline',
        detail: 'Launch a manual operator task',
        keywords: ['new', 'launch'],
        shortcut: 'Global N',
        disabled: projectDisabled,
        run: () => {
          openNewTaskModal();
          closePalette();
        },
      },
      {
        id: 'settings.general',
        label: 'Open global settings',
        section: 'Workbench',
        detail: 'Defaults, notifications, and controls',
        keywords: ['preferences', 'config'],
        shortcut: 'Global S',
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
    setViewMode,
    visibleActiveCount,
    visibleQueuedCount,
  ]);
}
