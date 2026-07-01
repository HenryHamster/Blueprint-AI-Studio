import { useCallback } from 'react';
import type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import { SETTINGS_SECTION_HOMES } from '../components/OperatorWorkspaceSections';
import type { SectionId } from '../components/settingsSections';
import type { PipelineSection } from './useWorkbenchCommands';
import type { PrimaryTab } from './useUIState';
import type { ViewMode } from './useViewPreferences';
import type { StrategicContextPackageDrilldown, StrategicMessageTarget } from '../strategicStateApi';
import { normalizeWorkbenchTarget } from '../types/workbenchTargets';

type StrategicContextTarget = StrategicContextPackageDrilldown['target'];

interface UseStrategicContextNavigationOptions {
  handleTabChange: (tab: PrimaryTab) => void;
  openMessages: (target?: StrategicMessageTarget) => void;
  openTask?: (taskId: string) => void;
  selectPipelineTaskById: (taskId: string) => void;
  setKnowledgeBrowserDirectionId: (directionId: string | null) => void;
  setKnowledgeBrowserInitialDocId: (documentId: string | null) => void;
  setPipelineSection: (section: PipelineSection) => void;
  setProjectSection: (section: ProjectSection) => void;
  setProjectManageSection: (section: ProjectManageSection) => void;
  setRuntimeSection: (section: RuntimeSection) => void;
  setRuntimeConfigSection: (section: RuntimeSettingsSection) => void;
  setShowArchive: (show: boolean) => void;
  setShowKnowledgeBrowser: (show: boolean) => void;
  setViewMode: (mode: ViewMode) => void;
}

function isProjectSection(value?: string | null): value is ProjectSection {
  return value === 'running-update' ||
    value === 'directions' ||
    value === 'problem-model' ||
    value === 'design-doc' ||
    value === 'manage';
}

function isRuntimeSection(value?: string | null): value is RuntimeSection {
  return value === 'health' ||
    value === 'devices' ||
    value === 'compute' ||
    value === 'accounts' ||
    value === 'settings' ||
    value === 'analytics';
}

function isSettingsSection(value?: string | null): value is SectionId {
  return typeof value === 'string' && value in SETTINGS_SECTION_HOMES;
}

function normalizePipelineSection(value?: string | null): PipelineSection {
  if (value === 'board' || value === 'archive' || value === 'flow') return value;
  return 'flow';
}

function messageTargetFromContextTarget(target: StrategicContextTarget): StrategicMessageTarget | undefined {
  const messageId = target.message_id ?? null;
  if (!messageId) return undefined;
  const action = target.action === 'reply' || target.action === 'highlight' ? target.action : undefined;
  return action ? { messageId, action } : { messageId };
}

export function useStrategicContextNavigation({
  handleTabChange,
  openMessages,
  openTask,
  selectPipelineTaskById,
  setKnowledgeBrowserDirectionId,
  setKnowledgeBrowserInitialDocId,
  setPipelineSection,
  setProjectSection,
  setProjectManageSection,
  setRuntimeSection,
  setRuntimeConfigSection,
  setShowArchive,
  setShowKnowledgeBrowser,
  setViewMode,
}: UseStrategicContextNavigationOptions) {
  return useCallback((target?: StrategicContextTarget | null) => {
    if (!target) {
      handleTabChange('chat');
      return;
    }
    const normalizedTarget = normalizeWorkbenchTarget(target) as StrategicContextTarget | null;
    if (!normalizedTarget) return;

    if (normalizedTarget.surface === 'project') {
      if (isSettingsSection(normalizedTarget.section)) {
        const home = SETTINGS_SECTION_HOMES[normalizedTarget.section];
        if (home.surface === 'project') {
          setProjectSection('manage');
          setProjectManageSection(home.section);
          handleTabChange('project');
          return;
        }
      }
      if (isProjectSection(normalizedTarget.section)) setProjectSection(normalizedTarget.section);
      handleTabChange('project');
      return;
    }
    if (normalizedTarget.surface === 'pipeline') {
      const section = normalizePipelineSection(normalizedTarget.section);
      setPipelineSection(section);
      if (section === 'board') setViewMode('kanban');
      if (section === 'flow') setViewMode('pipeline');
      setShowArchive(section === 'archive');
      if (normalizedTarget.task_id) selectPipelineTaskById(normalizedTarget.task_id);
      handleTabChange('pipeline');
      return;
    }
    if (normalizedTarget.surface === 'runtime') {
      if (isSettingsSection(normalizedTarget.section)) {
        const home = SETTINGS_SECTION_HOMES[normalizedTarget.section];
        if (home.surface === 'runtime') {
          setRuntimeSection(home.section);
          if (home.settingsSection) setRuntimeConfigSection(home.settingsSection);
          handleTabChange('runtime');
          return;
        }
      }
      setRuntimeSection(isRuntimeSection(normalizedTarget.section) ? normalizedTarget.section : 'health');
      handleTabChange('runtime');
      return;
    }
    if (normalizedTarget.surface === 'messages') {
      openMessages(messageTargetFromContextTarget(normalizedTarget));
      return;
    }
    if (normalizedTarget.surface === 'task') {
      setPipelineSection('flow');
      setViewMode('pipeline');
      setShowArchive(false);
      if (normalizedTarget.task_id) selectPipelineTaskById(normalizedTarget.task_id);
      if (normalizedTarget.task_id && openTask) {
        openTask(normalizedTarget.task_id);
        return;
      }
      handleTabChange('pipeline');
      return;
    }
    if (normalizedTarget.surface === 'knowledge') {
      setKnowledgeBrowserInitialDocId(normalizedTarget.document_id ?? null);
      setKnowledgeBrowserDirectionId(normalizedTarget.direction_id ?? null);
      setShowKnowledgeBrowser(true);
    }
  }, [
    handleTabChange,
    openMessages,
    openTask,
    selectPipelineTaskById,
    setKnowledgeBrowserDirectionId,
    setKnowledgeBrowserInitialDocId,
    setPipelineSection,
    setProjectSection,
    setProjectManageSection,
    setRuntimeSection,
    setRuntimeConfigSection,
    setShowArchive,
    setShowKnowledgeBrowser,
    setViewMode,
  ]);
}
