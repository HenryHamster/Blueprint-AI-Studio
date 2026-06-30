import { useCallback } from 'react';
import type { ProjectSection, RuntimeSection } from '../components/OperatorWorkspaces';
import type { PipelineSection } from './useWorkbenchCommands';
import type { PrimaryTab } from './useUIState';
import type { ViewMode } from './useViewPreferences';
import type { StrategicContextPackageDrilldown, StrategicMessageTarget } from '../strategicStateApi';

type StrategicContextTarget = StrategicContextPackageDrilldown['target'];

interface UseStrategicContextNavigationOptions {
  handleTabChange: (tab: PrimaryTab) => void;
  handleViewTaskById: (taskId: string) => void;
  openMessages: (target?: StrategicMessageTarget) => void;
  setKnowledgeBrowserDirectionId: (directionId: string | null) => void;
  setKnowledgeBrowserInitialDocId: (documentId: string | null) => void;
  setPipelineSection: (section: PipelineSection) => void;
  setProjectSection: (section: ProjectSection) => void;
  setRuntimeSection: (section: RuntimeSection) => void;
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
  handleViewTaskById,
  openMessages,
  setKnowledgeBrowserDirectionId,
  setKnowledgeBrowserInitialDocId,
  setPipelineSection,
  setProjectSection,
  setRuntimeSection,
  setShowArchive,
  setShowKnowledgeBrowser,
  setViewMode,
}: UseStrategicContextNavigationOptions) {
  return useCallback((target?: StrategicContextTarget | null) => {
    if (!target) {
      handleTabChange('chat');
      return;
    }
    if (target.surface === 'project') {
      if (isProjectSection(target.section)) setProjectSection(target.section);
      handleTabChange('project');
      return;
    }
    if (target.surface === 'pipeline') {
      const section = normalizePipelineSection(target.section);
      setPipelineSection(section);
      if (section === 'board') setViewMode('kanban');
      if (section === 'flow') setViewMode('pipeline');
      setShowArchive(section === 'archive');
      handleTabChange('pipeline');
      return;
    }
    if (target.surface === 'runtime') {
      setRuntimeSection(isRuntimeSection(target.section) ? target.section : 'health');
      handleTabChange('runtime');
      return;
    }
    if (target.surface === 'messages') {
      openMessages(messageTargetFromContextTarget(target));
      return;
    }
    if (target.surface === 'task') {
      if (target.task_id) handleViewTaskById(target.task_id);
      else handleTabChange('pipeline');
      return;
    }
    if (target.surface === 'knowledge') {
      setKnowledgeBrowserInitialDocId(target.document_id ?? null);
      setKnowledgeBrowserDirectionId(target.direction_id ?? null);
      setShowKnowledgeBrowser(true);
    }
  }, [
    handleTabChange,
    handleViewTaskById,
    openMessages,
    setKnowledgeBrowserDirectionId,
    setKnowledgeBrowserInitialDocId,
    setPipelineSection,
    setProjectSection,
    setRuntimeSection,
    setShowArchive,
    setShowKnowledgeBrowser,
    setViewMode,
  ]);
}
