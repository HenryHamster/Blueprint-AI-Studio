import { useCallback } from 'react';
import type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import { SETTINGS_SECTION_HOMES } from '../components/OperatorWorkspaceSections';
import type { SectionId } from '../components/settingsSections';
import type { PipelineSection } from './useWorkbenchCommands';
import type { PrimaryTab } from './useUIState';
import type { ViewMode } from './useViewPreferences';
import type { StrategicContextPackageDrilldown, StrategicMessageTarget } from '../strategicStateApi';
import {
  isWorkbenchPipelineSection,
  isWorkbenchProjectSection,
  isWorkbenchRuntimeSection,
  isWorkbenchTargetSurface,
  normalizeWorkbenchTarget,
} from '../types/workbenchTargets';

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

function isSettingsSection(value?: string | null): value is SectionId {
  return typeof value === 'string' && value in SETTINGS_SECTION_HOMES;
}

function normalizeSettingsTarget(target?: StrategicContextTarget | null): StrategicContextTarget | null {
  if (!target) return null;
  const surface = target.surface === 'project' || target.surface === 'runtime' ? target.surface : null;
  if (!surface || !isSettingsSection(target.section)) return null;
  const home = SETTINGS_SECTION_HOMES[target.section];
  if (home.surface !== surface) return null;
  return { ...target, surface, section: target.section };
}

function normalizeNavigationTarget(target?: StrategicContextTarget | null): StrategicContextTarget | null {
  return (normalizeWorkbenchTarget(target) as StrategicContextTarget | null) ?? normalizeSettingsTarget(target);
}

function isKnownNavigationSurface(value?: string | null): boolean {
  const surface = typeof value === 'string' ? value.trim() : null;
  return surface === 'chat' || surface === 'message' || isWorkbenchTargetSurface(surface);
}

function normalizePipelineSection(value?: string | null): PipelineSection {
  if (isWorkbenchPipelineSection(value)) return value;
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
    const normalizedTarget = normalizeNavigationTarget(target);
    if (!normalizedTarget) {
      if (!target || isKnownNavigationSurface(target.surface)) {
        handleTabChange('chat');
      }
      return;
    }
    target = normalizedTarget;
    if (target.surface === 'project') {
      if (target.section === 'overview') {
        setProjectSection('manage');
        setProjectManageSection('overview');
        handleTabChange('project');
        return;
      }
      if (isSettingsSection(target.section)) {
        const home = SETTINGS_SECTION_HOMES[target.section];
        if (home.surface === 'project') {
          setProjectSection('manage');
          setProjectManageSection(home.section);
          handleTabChange('project');
          return;
        }
      }
      if (isWorkbenchProjectSection(target.section) && target.section !== 'selected_refs') {
        setProjectSection(target.section as ProjectSection);
      }
      handleTabChange('project');
      return;
    }
    if (target.surface === 'pipeline') {
      const section = normalizePipelineSection(target.section);
      setPipelineSection(section);
      if (section === 'board') setViewMode('kanban');
      if (section === 'flow') setViewMode('pipeline');
      setShowArchive(section === 'archive');
      if (target.task_id) selectPipelineTaskById(target.task_id);
      handleTabChange('pipeline');
      return;
    }
    if (target.surface === 'runtime') {
      if (isSettingsSection(target.section)) {
        const home = SETTINGS_SECTION_HOMES[target.section];
        if (home.surface === 'runtime') {
          setRuntimeSection(home.section);
          if (home.settingsSection) setRuntimeConfigSection(home.settingsSection);
          handleTabChange('runtime');
          return;
        }
      }
      setRuntimeSection(isWorkbenchRuntimeSection(target.section) ? target.section as RuntimeSection : 'health');
      handleTabChange('runtime');
      return;
    }
    if (target.surface === 'messages') {
      openMessages(messageTargetFromContextTarget(target));
      return;
    }
    if (target.surface === 'task') {
      setPipelineSection('flow');
      setViewMode('pipeline');
      setShowArchive(false);
      if (target.task_id) selectPipelineTaskById(target.task_id);
      if (target.task_id && openTask) {
        openTask(target.task_id);
        return;
      }
      handleTabChange('pipeline');
      return;
    }
    if (target.surface === 'knowledge') {
      setKnowledgeBrowserInitialDocId(target.document_id ?? null);
      setKnowledgeBrowserDirectionId(target.direction_id ?? null);
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
