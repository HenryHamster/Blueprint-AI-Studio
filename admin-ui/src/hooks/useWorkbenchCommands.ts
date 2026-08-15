/* eslint-disable react-hooks/refs */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import { SETTINGS_SECTION_HOMES } from '../components/OperatorWorkspaceSections';
import type { SectionId } from '../components/settingsSections';
import type { StrategicInteractionMode } from '../utils/strategicWorkbenchSession';
import { executeStrategicWorkSessionCommand } from '../strategicStateApi';
import type {
  StrategicContextArtifactEvidence,
  StrategicContextCanvasNode,
  StrategicContextKnowledgeEvidence,
  StrategicContextPackageDescriptor,
  StrategicContextPackageDrilldown,
  StrategicContextResourceRef,
  StrategicMessageTarget,
  StrategicStateResponse,
  StrategicStateAttentionItem,
  StrategicStateMessage,
  StrategicStateTaskFocusItem,
  StrategicWorkbenchState,
  StrategicWorkSessionAction,
  StrategicWorkSessionCommand,
  StrategicWorkSessionRefDescriptor,
} from '../strategicStateApi';
import { formatStrategicMetricValue, rankStrategicWorkSessionMetrics } from '../utils/strategicWorkSessionMetrics';
import { removeStrategicSelectedRef, upsertStrategicSelectedRef, type StrategicSelectedRef } from '../utils/strategicWorkbenchSession';
import { normalizeWorkbenchRefetchSurfaces, normalizeWorkbenchTarget } from '../types/workbenchTargets';
import type { PrimaryTab } from './useUIState';
import type { ViewMode } from './useViewPreferences';
import { resolveAccess, type ProjectRole } from '../accessControl';

export type PipelineSection = 'flow' | 'board' | 'archive';
type StrategicContextTarget = StrategicContextPackageDrilldown['target'];
type StrategicContextCanvas = NonNullable<StrategicContextPackageDescriptor['canvas']>;
const WORK_SESSION_ACTION_COMMAND_LIMIT = 4;
const MESSAGE_COMMAND_LIMIT = 6;
const TASK_FOCUS_COMMAND_LIMIT = 6;
const STRATEGIC_INTERACTION_MODES: readonly StrategicInteractionMode[] = ['chat', 'continuous'];
const WORK_SESSION_MUTATION_METHODS = new Set(['PATCH', 'POST', 'PUT']);

type WorkSessionCommandRunStatus = {
  state: 'pending' | 'success' | 'error';
  message?: string;
};

interface UseWorkbenchCommandsOptions {
  selectedProjectId: string | null;
  selectedProjectRepoLabel: string | null;
  hasVersionLifecycle?: boolean;
  /** Current autonomous-mode state of the selected project (drives the
   *  "Toggle autonomous mode" command label/detail). */
  autonomousEnabled?: boolean;
  /** Flips the selected project's autonomous mode (PATCH .../autonomous).
   *  Absent when no toggle is wired; the command is then disabled. */
  toggleAutonomousMode?: () => void;
  directionsCount: number;
  filteredTaskCount: number;
  visibleActiveCount: number;
  visibleQueuedCount: number;
  maxPlanAccountCount: number;
  apiHealthy: boolean;
  isAdmin?: boolean;
  projectRole?: ProjectRole;
  problemModelRevision?: number | null;
  hasDesignDoc?: boolean;
  designRevision?: number | null;
  strategicInteractionMode?: StrategicInteractionMode;
  strategicState?: StrategicStateResponse | null;
  strategicWorkbenchState?: StrategicWorkbenchState | null;
  savingStrategicWorkbench?: boolean;
  checkingInStrategicSession?: boolean;
  switchStrategicInteractionMode?: (mode: StrategicInteractionMode) => void;
  markStrategicChecked?: () => void;
  focusStrategicComposer: () => void;
  openStrategicContextTarget?: (target?: StrategicContextTarget | null) => void;
  openStrategicContextWorkspace?: () => void;
  openStrategicMessageTarget?: (target?: StrategicMessageTarget) => void;
  updateStrategicSessionRefs?: (
    updateRefs: (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs']
  ) => void;
  handleTabChange: (tab: PrimaryTab) => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setPipelineSection: (section: PipelineSection) => void;
  setProjectSection: (section: ProjectSection) => void;
  setProjectManageSection: (section: ProjectManageSection) => void;
  setRuntimeSection: (section: RuntimeSection) => void;
  setRuntimeConfigSection: (section: RuntimeSettingsSection) => void;
  setViewMode: (mode: ViewMode) => void;
  setShowArchive: (show: boolean) => void;
  setShowActivityFeed: (show: boolean) => void;
  setShowResearchBrowser: (show: boolean) => void;
  setShowKnowledgeBrowser: (show: boolean) => void;
  openNewTaskModal: () => void;
  refreshSettings: () => void;
  refreshWorkbench: () => void;
  openProjectManagement: () => void;
  openProjectMembers: () => void;
  openPreviousWorkbenchSubtab?: () => void;
  openNextWorkbenchSubtab?: () => void;
}

function contextKindLabel(kind?: string | null): string {
  if (kind === 'knowledge_document') return 'knowledge';
  if (kind === 'canvas') return 'context map';
  return (kind || 'context').replace(/_/g, ' ');
}

function contextRefKey(ref: Pick<StrategicSelectedRef, 'kind' | 'id'>): string {
  return `${ref.kind}:${ref.id}`;
}

function selectedRefFromResource(resource: StrategicContextResourceRef): StrategicSelectedRef {
  return {
    kind: resource.kind,
    id: resource.id,
    label: resource.label,
  };
}

function selectedRefFromCanvas(canvas: StrategicContextCanvas): StrategicSelectedRef | null {
  if (!canvas.id) return null;
  return {
    kind: 'canvas',
    id: canvas.id,
    label: canvas.title ?? 'Strategic context map',
  };
}

function selectedRefFromKnowledgeEvidence(item: StrategicContextKnowledgeEvidence): StrategicSelectedRef {
  return {
    kind: 'knowledge_document',
    id: item.id.startsWith('knowledge:') ? item.id : `knowledge:${item.id}`,
    label: item.label ?? item.topic ?? 'Knowledge document',
  };
}

function selectedRefFromArtifactEvidence(item: StrategicContextArtifactEvidence): StrategicSelectedRef {
  return {
    kind: 'artifact',
    id: item.id.startsWith('artifact:') ? item.id : `artifact:${item.id}`,
    label: item.label ?? item.logical_key ?? item.artifact_type ?? 'Artifact',
  };
}

function selectedRefFromNode(node: StrategicContextCanvasNode): StrategicSelectedRef {
  return {
    kind: node.kind,
    id: node.id,
    label: node.label,
  };
}

function selectedRefFromDescriptor(ref: StrategicWorkSessionRefDescriptor): StrategicSelectedRef {
  return {
    kind: ref.kind,
    id: ref.id,
    label: ref.label,
  };
}

function fallbackSelectedRefLabel(ref: StrategicSelectedRef): string {
  return ref.label?.trim() || `${contextKindLabel(ref.kind)} ${ref.id.slice(0, 8)}`;
}

function formatRelativeTime(timestamp?: string | null): string | null {
  if (!timestamp) return null;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return null;
  const diffMs = Date.now() - date.getTime();
  if (diffMs < 60_000) return 'just now';
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString();
}

function checkInChangeCount(workSession: StrategicStateResponse['work_session'] | undefined | null): number | null {
  const since = workSession?.since_last_check_in;
  if (!since?.available) return null;
  return (
    (since.messages?.total ?? 0)
    + (since.tasks?.updated ?? 0)
    + (since.alerts?.raised ?? 0)
    + (since.strategic?.cycles_completed ?? 0)
  );
}

function checkInCommandDetail(workSession: StrategicStateResponse['work_session'] | undefined | null): string {
  const lastCheckIn = workSession?.last_check_in;
  if (!lastCheckIn?.created_at) return 'Never checked';
  const relative = formatRelativeTime(lastCheckIn.created_at);
  const changeCount = checkInChangeCount(workSession);
  const changeLabel = changeCount == null ? null : `${changeCount} new`;
  return [relative ? `Last ${relative}` : null, changeLabel].filter(Boolean).join(' / ') || 'Checked';
}

function compactContextDetail(...parts: Array<string | null | undefined>): string | null {
  const detailParts = parts.map(part => part?.trim()).filter((part): part is string => Boolean(part));
  return detailParts.length > 0 ? detailParts.join(' / ') : null;
}

function canvasSummaryDetail(
  canvas: StrategicContextCanvas | null,
  nodeCount: number,
  edgeCount: number,
): string {
  const nodes = canvas?.node_count ?? nodeCount;
  const edges = canvas?.edge_count ?? edgeCount;
  return `${nodes} nodes / ${edges} links`;
}

function attentionItemLabel(item: StrategicStateAttentionItem): string {
  const label = item.label?.trim() || item.detail?.trim() || 'attention item';
  if (item.kind === 'message' && item.reason === 'question') return `Reply to question: ${label}`;
  if (item.kind === 'message') return `Open message: ${label}`;
  if (item.kind === 'task') return `Open attention task: ${label}`;
  if (item.kind === 'direction') return `Open direction: ${label}`;
  if (item.kind === 'alert') return `Open alert: ${label}`;
  return `Open attention: ${label}`;
}

function messageCommandLabel(message: StrategicStateMessage): string {
  const content = message.content?.trim();
  if (message.message_type === 'QUESTION') return `Reply to message: ${content || 'Question waiting'}`;
  if (message.message_type === 'BLOCKER') return `Open message: ${content || 'Blocker'}`;
  if (message.message_type === 'BUG_REPORT') return `Open message: ${content || 'Bug report'}`;
  return `Open message: ${content || message.message_type || 'Message'}`;
}

function messageCommandDetail(message: StrategicStateMessage): string | undefined {
  return compactContextDetail(message.message_type, message.from_type, message.status) ?? undefined;
}

function messageCommandAction(message: StrategicStateMessage): StrategicMessageTarget['action'] {
  return message.message_type === 'QUESTION' ? 'reply' : 'highlight';
}

function taskFocusBucketLabel(bucket: string): string {
  if (bucket === 'needs_judgment') return 'Needs judgment';
  if (bucket === 'active') return 'Pipeline';
  if (bucket === 'friction') return 'Friction';
  if (bucket === 'queued') return 'Queued';
  if (bucket === 'recent_completed') return 'Recent result';
  return bucket.replace(/_/g, ' ');
}

function taskFocusCommandLabel(item: StrategicStateTaskFocusItem): string {
  return `Open task: ${item.title?.trim() || item.task_type || item.id.slice(0, 8)}`;
}

function taskFocusCommandDetail(item: StrategicStateTaskFocusItem): string | undefined {
  return compactContextDetail(
    taskFocusBucketLabel(item.bucket),
    item.status,
    item.movement_explanation
      ?? item.detail
      ?? item.latest_status_message
      ?? item.blocked_reason
      ?? item.park_reason,
  ) ?? undefined;
}

function workSessionActionLabel(action: StrategicWorkSessionAction): string {
  const label = action.label?.trim();
  if (label) return label;
  if (action.kind === 'reply') return 'Reply to message';
  if (action.kind === 'review') return 'Review item';
  if (action.kind === 'approve') return 'Review approval';
  if (action.kind === 'inspect') return 'Inspect target';
  if (action.kind === 'resume') return 'Resume work';
  return 'Open action';
}

function workSessionActionDetail(action: StrategicWorkSessionAction): string | undefined {
  const detail = (!action.enabled && action.blocked_reason ? action.blocked_reason : action.reason)?.trim();
  const priority = action.priority === 'critical' || action.priority === 'high' ? action.priority : null;
  return compactContextDetail(detail, priority) ?? undefined;
}

function workSessionMessageIntent(action: StrategicWorkSessionAction): StrategicMessageTarget['action'] {
  const targetAction = action.target?.action;
  if (targetAction === 'reply' || targetAction === 'highlight') return targetAction;
  return action.kind === 'reply' ? 'reply' : 'highlight';
}

function workSessionActionMessageId(action: StrategicWorkSessionAction): string | null {
  const messageId = action.target?.message_id?.trim();
  return messageId || null;
}

function workSessionActionIsMessageTarget(action: StrategicWorkSessionAction): boolean {
  const target = normalizeStrategicContextTarget(action.target);
  return Boolean(workSessionActionMessageId(action) || target?.surface === 'messages');
}

function workSessionActionContextTarget(action: StrategicWorkSessionAction): StrategicContextTarget | null {
  const target = normalizeStrategicContextTarget(action.target);
  if (target) return target;
  if (action.target?.task_id) return normalizeStrategicContextTarget({ ...action.target, surface: 'task' });
  return null;
}

function isSettingsSection(value?: string | null): value is SectionId {
  return typeof value === 'string' && value in SETTINGS_SECTION_HOMES;
}

function normalizeSettingsContextTarget(target?: StrategicContextTarget | null): StrategicContextTarget | null {
  if (!target) return null;
  const surface = target.surface === 'project' || target.surface === 'runtime' ? target.surface : null;
  if (!surface || !isSettingsSection(target.section)) return null;
  const home = SETTINGS_SECTION_HOMES[target.section];
  if (home.surface !== surface) return null;
  return { ...target, surface, section: target.section };
}

function normalizeStrategicContextTarget(target?: StrategicContextTarget | null): StrategicContextTarget | null {
  return (normalizeWorkbenchTarget(target) as StrategicContextTarget | null) ?? normalizeSettingsContextTarget(target);
}

function normalizeWorkSessionCommandDescriptor(command: StrategicWorkSessionCommand): StrategicWorkSessionCommand | null {
  const target = normalizeStrategicContextTarget(command.target ?? null);
  if (!target) return null;
  return {
    ...command,
    target,
    refetch_surfaces: normalizeWorkbenchRefetchSurfaces(command.refetch_surfaces),
    executable: command.executable === false ? false : Boolean(command.mutation),
  };
}

function isStrategicInteractionMode(value: string | null | undefined): value is StrategicInteractionMode {
  return Boolean(value && (STRATEGIC_INTERACTION_MODES as readonly string[]).includes(value));
}

function strategicModeFromCommandId(commandId: string): StrategicInteractionMode | null {
  if (!commandId.startsWith('session.mode.')) return null;
  const mode = commandId.slice('session.mode.'.length);
  return isStrategicInteractionMode(mode) ? mode : null;
}

function isStrategicSessionMutationCommandId(commandId: string): boolean {
  return (
    Boolean(strategicModeFromCommandId(commandId))
    || commandId === 'session.clear_selected_refs'
    || commandId.startsWith('session.runner.prefer.')
    || commandId === 'session.runner.clear_preferred'
  );
}

function isAllowedWorkSessionMutationCommandId(commandId: string): boolean {
  return (
    isStrategicSessionMutationCommandId(commandId)
    || commandId === 'attention.messages.mark_seen'
    || commandId.startsWith('attention.alert.dismiss:')
  );
}

function isStrategicSessionMutationPath(path: string, projectId: string): boolean {
  const prefix = `/api/admin/projects/${encodeURIComponent(projectId)}/strategic-session`;
  return path === prefix || path.startsWith(`${prefix}?`);
}

function isMessagesLastSeenMutation(command: StrategicWorkSessionCommand, method: string, path: string, projectId: string): boolean {
  return (
    command.id === 'attention.messages.mark_seen'
    && method === 'PUT'
    && path === '/api/admin/messages/last-seen'
    && command.target?.surface === 'messages'
    && command.target?.project_id === projectId
    && command.target?.action === 'mark_seen'
    && command.mutation?.body?.project_id === projectId
  );
}

function isAlertDismissMutation(command: StrategicWorkSessionCommand, method: string, path: string, projectId: string): boolean {
  if (!command.id.startsWith('attention.alert.dismiss:') || method !== 'POST') return false;
  const alertId = command.id.slice('attention.alert.dismiss:'.length);
  return Boolean(
    alertId
      && command.target?.project_id === projectId
      && command.target?.surface === 'runtime'
      && command.target?.section === 'health'
      && command.target?.action === 'dismiss_alert'
      && command.target?.alert_id === alertId
      && path === `/api/admin/alerts/${encodeURIComponent(alertId)}/dismiss`,
  );
}

function hasAllowedWorkSessionMutation(command: StrategicWorkSessionCommand, projectId: string | null): boolean {
  const mutation = command.mutation;
  const method = mutation?.method?.trim().toUpperCase();
  const path = mutation?.path?.trim();
  if (!projectId || !mutation || !method || !WORK_SESSION_MUTATION_METHODS.has(method) || !path) {
    return false;
  }
  if (command.executable === false) return false;
  if (!isAllowedWorkSessionMutationCommandId(command.id)) return false;
  if (isMessagesLastSeenMutation(command, method, path, projectId)) return true;
  if (isAlertDismissMutation(command, method, path, projectId)) return true;
  return (
    isStrategicSessionMutationCommandId(command.id)
    && isStrategicSessionMutationPath(path, projectId)
  );
}

function humanizeCommandToken(value?: string | null): string | null {
  const text = value?.replace(/[._:-]+/g, ' ').replace(/\s+/g, ' ').trim();
  return text || null;
}

function workSessionCommandLabel(command: StrategicWorkSessionCommand): string {
  return command.label?.trim() || humanizeCommandToken(command.id) || 'Open command';
}

function workSessionCommandPriorityDetail(priority?: string | null): string | null {
  return priority === 'critical' || priority === 'high' ? priority : null;
}

function workSessionCommandTargetDetail(target?: StrategicContextTarget | null): string | null {
  const normalizedTarget = normalizeStrategicContextTarget(target);
  if (!normalizedTarget) return null;
  const section = humanizeCommandToken(normalizedTarget.section);
  if (normalizedTarget.surface === 'messages') return normalizedTarget.message_id ? 'Message' : 'Messages';
  if (normalizedTarget.surface === 'task') return 'Task';
  if (normalizedTarget.surface === 'runtime' && normalizedTarget.action === 'dismiss_alert' && normalizedTarget.alert_id) {
    const alertRef = normalizedTarget.alert_id.slice(0, 8);
    return section ? `Runtime ${section} / alert ${alertRef}` : `Runtime alert ${alertRef}`;
  }
  if (normalizedTarget.surface === 'runtime') return section ? `Runtime ${section}` : 'Runtime';
  if (normalizedTarget.surface === 'pipeline') return section ? `Pipeline ${section}` : 'Pipeline';
  if (normalizedTarget.surface === 'project') return section ? `Project ${section}` : 'Project';
  if (normalizedTarget.surface === 'knowledge') return 'Knowledge';
  return null;
}

function workSessionCommandSection(command: StrategicWorkSessionCommand): string {
  const group = command.group?.trim();
  const surface = normalizeStrategicContextTarget(command.target)?.surface;
  if (command.id === 'session.check_in' || command.id.startsWith('session.mode.') || group === 'session' || group === 'mode') {
    return 'Strategic';
  }
  if (surface === 'messages' || group === 'message') return 'Messages';
  if (group === 'attention') return 'Attention';
  if (surface === 'task' || group === 'task') return 'Tasks';
  if (surface === 'runtime' || group === 'runtime') return 'Runtime';
  if (surface === 'pipeline') return 'Pipeline';
  if (surface === 'project' || surface === 'knowledge' || group === 'context') return 'Context';
  return 'Session';
}

function messageTargetFromCommand(command: StrategicWorkSessionCommand): StrategicMessageTarget | undefined {
  const target = normalizeStrategicContextTarget(command.target);
  const messageId = target?.message_id?.trim();
  if (!messageId) return undefined;
  const action = target?.action;
  if (action === 'reply' || action === 'highlight') return { messageId, action };
  return { messageId };
}

function workSessionCommandIdempotencyKey(commandId: string): string {
  const randomValue = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2);
  const compactCommandId = commandId.replace(/\s+/g, '-').slice(0, 140);
  return `work-session-command:${compactCommandId}:${Date.now().toString(36)}:${randomValue}`.slice(0, 255);
}

function commandExecutionErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  if (typeof error === 'string' && error.trim()) return error.trim();
  return 'Command failed';
}

function workSessionCommandStatusKey(projectId: string | null, commandId: string): string {
  return `${projectId ?? 'no-project'}:${commandId}`;
}

function workSessionCommandExecutionPayload(command: StrategicWorkSessionCommand) {
  const payload: { idempotency_key: string; observed_at?: number } = {
    idempotency_key: workSessionCommandIdempotencyKey(command.id),
  };
  const timestamp = command.mutation?.body?.timestamp;
  if (command.id === 'attention.messages.mark_seen' && typeof timestamp === 'number') {
    payload.observed_at = timestamp;
  }
  return payload;
}

export function useWorkbenchCommands({
  selectedProjectId,
  selectedProjectRepoLabel,
  hasVersionLifecycle = false,
  autonomousEnabled = false,
  toggleAutonomousMode,
  directionsCount,
  filteredTaskCount,
  visibleActiveCount,
  visibleQueuedCount,
  maxPlanAccountCount,
  apiHealthy,
  isAdmin = false,
  projectRole,
  problemModelRevision,
  hasDesignDoc,
  designRevision,
  strategicInteractionMode,
  strategicState,
  strategicWorkbenchState,
  savingStrategicWorkbench = false,
  checkingInStrategicSession = false,
  switchStrategicInteractionMode,
  markStrategicChecked,
  focusStrategicComposer,
  openStrategicContextTarget,
  openStrategicContextWorkspace,
  openStrategicMessageTarget,
  updateStrategicSessionRefs,
  handleTabChange,
  setCommandPaletteOpen,
  setPipelineSection,
  setProjectSection,
  setProjectManageSection,
  setRuntimeSection,
  setRuntimeConfigSection,
  setViewMode,
  setShowArchive,
  setShowActivityFeed,
  setShowResearchBrowser,
  setShowKnowledgeBrowser,
  openNewTaskModal,
  refreshSettings,
  refreshWorkbench,
  openProjectManagement,
  openProjectMembers,
  openPreviousWorkbenchSubtab,
  openNextWorkbenchSubtab,
}: UseWorkbenchCommandsOptions): WorkbenchCommand[] {
  const [workSessionCommandStatuses, setWorkSessionCommandStatuses] = useState<Record<string, WorkSessionCommandRunStatus>>({});
  const projectAccess = resolveAccess(undefined, projectRole);
  const canOperateProjectRef = useRef(projectAccess.canOperateProject);
  useEffect(() => {
    canOperateProjectRef.current = projectAccess.canOperateProject;
  }, [projectAccess.canOperateProject]);

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
    const openProjectManageSection = (section: ProjectManageSection) => {
      setProjectSection('manage');
      setProjectManageSection(section);
      handleTabChange('project');
      closePalette();
    };
    const openRuntimeSection = (section: RuntimeSection) => {
      setRuntimeSection(section);
      handleTabChange('runtime');
      closePalette();
    };
    const openRuntimeSettingsSection = (section: RuntimeSettingsSection) => {
      refreshSettings();
      setRuntimeSection('settings');
      setRuntimeConfigSection(section);
      handleTabChange('runtime');
      closePalette();
    };
    const openSettingsSectionHome = (section: SectionId) => {
      const home = SETTINGS_SECTION_HOMES[section];
      if (home.surface === 'project') {
        openProjectManageSection(home.section);
        return;
      }
      if (home.section === 'accounts') {
        openRuntimeSection('accounts');
        return;
      }
      if (home.settingsSection) openRuntimeSettingsSection(home.settingsSection);
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
      if (!canOperateProjectRef.current) return;
      switchStrategicInteractionMode?.(mode);
      closePalette();
    };
    const selectedRefs = strategicWorkbenchState?.selected_refs ?? strategicState?.work_session?.selected_refs ?? strategicState?.workbench?.selected_refs ?? [];
    const selectedKeys = new Set(selectedRefs.map(contextRefKey));
    const contextPackage = strategicState?.context_package ?? null;
    const canvas = contextPackage?.canvas ?? null;
    const canvasNodeCount = canvas?.node_count ?? canvas?.nodes?.length ?? 0;
    const canvasEdgeCount = canvas?.edge_count ?? canvas?.edges?.length ?? 0;
    const resources = contextPackage?.resources ?? [];
    const canvasResources = resources.filter(resource => resource.kind === 'canvas');
    const listedResources = resources.filter(resource => resource.kind !== 'canvas');
    const syntheticCanvasRef = canvas ? selectedRefFromCanvas(canvas) : null;
    const canvasResourceRows = canvasResources.length > 0
      ? canvasResources
      : syntheticCanvasRef
        ? [{
            kind: 'canvas',
            id: syntheticCanvasRef.id,
            label: syntheticCanvasRef.label ?? 'Strategic context map',
            summary: canvasSummaryDetail(canvas, canvasNodeCount, canvasEdgeCount),
          } satisfies StrategicContextResourceRef]
        : [];
    const contextCommandEntries: WorkbenchCommand[] = [];
    const seenContextCommandIds = new Set<string>();
    const seenContextOpenRefs = new Set<string>();
    const seenContextActionRefs = new Set<string>();
    const addContextCommand = (command: WorkbenchCommand) => {
      if (seenContextCommandIds.has(command.id)) return;
      seenContextCommandIds.add(command.id);
      contextCommandEntries.push(command);
    };
    const openContextTarget = (target?: StrategicContextTarget | null) => {
      openStrategicContextTarget?.(normalizeStrategicContextTarget(target));
      closePalette();
    };
    const openAttentionItem = (item: StrategicStateAttentionItem) => {
      const target = normalizeStrategicContextTarget(item.target);
      if (target && openStrategicContextTarget) {
        openContextTarget(target);
        return;
      }
      if (item.kind === 'message') {
        const messageId = item.ref?.id;
        if (openStrategicMessageTarget) {
          openStrategicMessageTarget(messageId ? {
            messageId,
            action: item.reason === 'question' ? 'reply' : 'highlight',
          } : undefined);
          closePalette();
        } else {
          openContextTarget({ surface: 'messages' });
        }
        return;
      }
      if (item.kind === 'task') {
        openContextTarget({ surface: 'task', task_id: item.task_id ?? item.ref?.id });
        return;
      }
      if (item.kind === 'direction') {
        openProjectSection('directions');
        return;
      }
      if (item.kind === 'alert' && item.task_id) {
        openContextTarget({ surface: 'task', task_id: item.task_id });
        return;
      }
      if (item.kind === 'alert') {
        openRuntimeSection('health');
        return;
      }
      runAndClose(focusStrategicComposer);
    };
    const seenWorkSessionCommandIds = new Set<string>();
    const workSessionCommandDescriptors = (strategicState?.work_session?.commands ?? []).reduce<StrategicWorkSessionCommand[]>((acc, command) => {
      const id = command.id?.trim();
      if (!id || seenWorkSessionCommandIds.has(id)) return acc;
      const normalizedCommand = normalizeWorkSessionCommandDescriptor(command);
      if (!normalizedCommand) return acc;
      seenWorkSessionCommandIds.add(id);
      acc.push(normalizedCommand);
      return acc;
    }, []);
    const workSessionCommandIds = seenWorkSessionCommandIds;
    const workSessionCommandModes = new Set<StrategicInteractionMode>();
    const workSessionCommandMessageIds = new Set<string>();
    workSessionCommandDescriptors.forEach(command => {
      const mode = strategicModeFromCommandId(command.id);
      if (mode) workSessionCommandModes.add(mode);
      const messageId = command.target?.message_id?.trim();
      if (messageId) workSessionCommandMessageIds.add(messageId);
    });
    const legacyWorkSessionActions = (strategicState?.work_session?.actions ?? [])
      .filter(action => !workSessionCommandIds.has(action.id));
    const visibleWorkSessionActions = legacyWorkSessionActions.slice(0, WORK_SESSION_ACTION_COMMAND_LIMIT);
    const strategicCheckInDetail = checkingInStrategicSession ? 'Recording check-in' : checkInCommandDetail(strategicState?.work_session);
    const visibleEnabledWorkSessionActionIds = new Set(
      visibleWorkSessionActions.filter(action => action.enabled).map(action => action.id),
    );
    const coveredMessageIds = new Set(
      visibleWorkSessionActions
        .map(action => workSessionActionMessageId(action))
        .filter((id): id is string => Boolean(id)),
    );
    workSessionCommandMessageIds.forEach(messageId => coveredMessageIds.add(messageId));
    (strategicState?.attention?.items ?? []).forEach(item => {
      if (item.kind === 'message' && item.ref?.id) coveredMessageIds.add(item.ref.id);
    });
    const workSessionActionCanRoute = (action: StrategicWorkSessionAction): boolean => {
      if (workSessionActionIsMessageTarget(action)) return Boolean(openStrategicMessageTarget || openStrategicContextTarget);
      return Boolean(openStrategicContextTarget && workSessionActionContextTarget(action));
    };
    const openWorkSessionAction = (action: StrategicWorkSessionAction) => {
      if (!action.enabled) {
        closePalette();
        return;
      }

      const target = normalizeStrategicContextTarget(action.target);
      const messageId = workSessionActionMessageId(action);
      if (messageId || target?.surface === 'messages') {
        if (openStrategicMessageTarget) {
          openStrategicMessageTarget(messageId ? {
            messageId,
            action: workSessionMessageIntent(action),
          } : undefined);
          closePalette();
        } else {
          openContextTarget(target?.surface ? target : { surface: 'messages', message_id: messageId });
        }
        return;
      }

      const contextTarget = workSessionActionContextTarget(action);
      if (contextTarget) {
        openContextTarget(contextTarget);
        return;
      }

      closePalette();
    };
    const commandHasAllowedMutation = (command: StrategicWorkSessionCommand): boolean => (
      hasAllowedWorkSessionMutation(command, selectedProjectId)
    );
    const commandRequiresOperate = (command: StrategicWorkSessionCommand): boolean => (
      commandHasAllowedMutation(command)
      || command.id === 'session.check_in'
      || command.id.startsWith('session.mode.')
      || command.id === 'session.clear_selected_refs'
    );
    const workSessionCommandCanRoute = (command: StrategicWorkSessionCommand): boolean => {
      if (commandHasAllowedMutation(command)) return true;
      if (command.id === 'session.check_in') return Boolean(markStrategicChecked);
      if (command.id.startsWith('session.mode.')) return Boolean(strategicModeFromCommandId(command.id));
      if (command.id === 'session.clear_selected_refs') return Boolean(updateStrategicSessionRefs);
      const target = normalizeStrategicContextTarget(command.target);
      if (target?.surface === 'messages') return Boolean(openStrategicMessageTarget || openStrategicContextTarget);
      return Boolean(target && openStrategicContextTarget);
    };
    const workSessionDescriptorCommandDetail = (
      command: StrategicWorkSessionCommand,
      routeable: boolean,
      runStatus?: WorkSessionCommandRunStatus,
    ): string | undefined => {
      if (runStatus?.state === 'pending') return 'Running command...';
      if (runStatus?.state === 'error') return `Failed: ${runStatus.message ?? 'Command failed'}`;
      if (runStatus?.state === 'success') return runStatus.message ?? 'Completed';
      const mode = strategicModeFromCommandId(command.id);
      if (command.id === 'session.check_in') return strategicCheckInDetail;
      if (mode) {
        if (mode === strategicInteractionMode) return 'Current mode';
        if (savingStrategicWorkbench) return 'Saving session';
        if (!command.enabled) return 'Unavailable';
        return 'Set session mode';
      }
      if (command.id === 'session.clear_selected_refs') {
        if (selectedRefs.length === 0) return 'No selected refs';
        return `${selectedRefs.length} selected`;
      }
      if (!routeable) return 'No navigation target';
      return compactContextDetail(
        workSessionCommandTargetDetail(command.target),
        workSessionCommandPriorityDetail(command.priority),
      ) ?? undefined;
    };
    const runWorkSessionCommandMutation = (command: StrategicWorkSessionCommand): boolean => {
      if (
        !canOperateProjectRef.current
        || !selectedProjectId
        || !commandHasAllowedMutation(command)
        || !command.mutation
      ) return false;

      setWorkSessionCommandStatuses(prev => ({
        ...prev,
        [workSessionCommandStatusKey(selectedProjectId, command.id)]: { state: 'pending' },
      }));
      void executeStrategicWorkSessionCommand(
        selectedProjectId,
        command.id,
        workSessionCommandExecutionPayload(command),
      )
        .then(response => {
          const receipt = response.command.receipt;
          if (response.command.status === 'failed' || receipt.status === 'failed') {
            if (typeof refreshWorkbench === 'function') refreshWorkbench();
            throw new Error(receipt.summary ?? 'Command failed');
          }
          setWorkSessionCommandStatuses(prev => ({
            ...prev,
            [workSessionCommandStatusKey(selectedProjectId, command.id)]: {
              state: 'success',
              message: receipt.summary ?? 'Completed',
            },
          }));
          if (typeof refreshWorkbench === 'function') refreshWorkbench();
          closePalette();
        })
        .catch(error => {
          setWorkSessionCommandStatuses(prev => ({
            ...prev,
            [workSessionCommandStatusKey(selectedProjectId, command.id)]: {
              state: 'error',
              message: commandExecutionErrorMessage(error),
            },
          }));
        })
        .catch(() => undefined);
      return true;
    };
    const runWorkSessionCommandDescriptor = (command: StrategicWorkSessionCommand, disabled: boolean) => {
      if (disabled) {
        closePalette();
        return;
      }

      if (runWorkSessionCommandMutation(command)) return;

      if (command.id === 'session.check_in') {
        markStrategicChecked?.();
        closePalette();
        return;
      }

      const mode = strategicModeFromCommandId(command.id);
      if (mode) {
        setStrategicModeAndOpenChat(mode);
        return;
      }

      if (command.id === 'session.clear_selected_refs') {
        updateStrategicSessionRefs?.(() => []);
        closePalette();
        return;
      }

      const target = normalizeStrategicContextTarget(command.target);
      if (target?.surface === 'messages') {
        if (openStrategicMessageTarget) {
          openStrategicMessageTarget(messageTargetFromCommand(command));
          closePalette();
        } else {
          openContextTarget(target);
        }
        return;
      }

      if (target) {
        openContextTarget(target);
        return;
      }

      closePalette();
    };
    const workSessionDescriptorCommands = workSessionCommandDescriptors.map((command): WorkbenchCommand => {
      const routeable = workSessionCommandCanRoute(command);
      const mode = strategicModeFromCommandId(command.id);
      const runStatus = workSessionCommandStatuses[workSessionCommandStatusKey(selectedProjectId, command.id)];
      const disabled = projectDisabled
        || (commandRequiresOperate(command) && !projectAccess.canOperateProject)
        || !command.enabled
        || !routeable
        || runStatus?.state === 'pending'
        || (command.id === 'session.check_in' && checkingInStrategicSession)
        || (Boolean(mode) && (savingStrategicWorkbench || mode === strategicInteractionMode))
        || (command.id === 'session.clear_selected_refs' && savingStrategicWorkbench);
      return {
        id: command.id,
        label: workSessionCommandLabel(command),
        section: workSessionCommandSection(command),
        detail: workSessionDescriptorCommandDetail(command, routeable, runStatus),
        keywords: [
          'strategic',
          'session',
          'command',
          command.id,
          command.group,
          command.priority,
          command.label,
          command.target?.surface ?? '',
          command.target?.section ?? '',
          command.target?.task_id ?? '',
          command.target?.message_id ?? '',
          command.target?.document_id ?? '',
          command.target?.artifact_id ?? '',
        ],
        disabled,
        run: () => runWorkSessionCommandDescriptor(command, disabled),
      };
    });
    const workSessionActionCommands = visibleWorkSessionActions.map((action): WorkbenchCommand => {
      const routeable = workSessionActionCanRoute(action);
      const disabled = projectDisabled || !action.enabled || !routeable;
      return {
        id: `strategic.session.action.${action.id}`,
        label: workSessionActionLabel(action),
        section: 'Session',
        detail: workSessionActionDetail(action) ?? (!routeable ? 'No navigation target' : undefined),
        keywords: [
          'strategic',
          'session',
          'action',
          action.kind,
          action.source,
          action.priority,
          action.label,
          action.reason,
          action.blocked_reason ?? '',
          action.target?.surface ?? '',
          action.target?.section ?? '',
          action.target?.task_id ?? '',
          action.target?.message_id ?? '',
          action.target?.document_id ?? '',
          action.target?.artifact_id ?? '',
        ],
        disabled,
        run: () => openWorkSessionAction(action),
      };
    });
    const keepContextRef = (ref: StrategicSelectedRef) => {
      if (!canOperateProjectRef.current) return;
      updateStrategicSessionRefs?.(refs => upsertStrategicSelectedRef(refs, ref));
      closePalette();
    };
    const removeContextRef = (ref: Pick<StrategicSelectedRef, 'kind' | 'id'>) => {
      if (!canOperateProjectRef.current) return;
      updateStrategicSessionRefs?.(refs => removeStrategicSelectedRef(refs, ref));
      closePalette();
    };
    const addRefCommands = (source: {
      ref: StrategicSelectedRef;
      label: string;
      detail?: string | null;
      target?: StrategicContextTarget | null;
      origin: 'kept' | 'resource' | 'map' | 'evidence';
      allowOpen?: boolean;
    }) => {
      const key = contextRefKey(source.ref);
      const selected = selectedKeys.has(key);
      const detail = compactContextDetail(contextKindLabel(source.ref.kind), source.detail) ?? contextKindLabel(source.ref.kind);
      if (source.allowOpen !== false && source.target && openStrategicContextTarget && !seenContextOpenRefs.has(key)) {
        seenContextOpenRefs.add(key);
        addContextCommand({
          id: `strategic.context.open.${source.origin}.${key}`,
          label: source.origin === 'kept' ? `Open kept context: ${source.label}` : `Open context: ${source.label}`,
          section: 'Context',
          detail,
          keywords: ['strategic', 'context', source.origin, source.ref.kind, source.label],
          disabled: projectDisabled,
          run: () => openContextTarget(source.target),
        });
      }
      if (updateStrategicSessionRefs && !seenContextActionRefs.has(key)) {
        seenContextActionRefs.add(key);
        addContextCommand({
          id: `strategic.context.${selected ? 'remove' : 'keep'}.${key}`,
          label: `${selected ? 'Remove from' : 'Keep in'} Strategic context: ${source.label}`,
          section: 'Context',
          detail,
          keywords: ['strategic', 'context', selected ? 'remove' : 'keep', source.ref.kind, source.label],
          disabled: projectDisabled || !projectAccess.canOperateProject || savingStrategicWorkbench,
          run: selected ? () => removeContextRef(source.ref) : () => keepContextRef(source.ref),
        });
      }
    };
    if (contextPackage && !workSessionCommandIds.has('context.canvas.open')) {
      const resourceCount = listedResources.length;
      const mapTarget = canvasResources.find(resource => resource.target)?.target
        ?? { surface: 'project', section: 'problem-model' };
      addContextCommand({
        id: 'strategic.context.map',
        label: 'Strategic context map',
        section: 'Context',
        detail: `${selectedRefs.length} kept, ${resourceCount} resources, ${canvasNodeCount} nodes`,
        keywords: ['strategic', 'context', 'map', 'canvas', 'kept'],
        disabled: projectDisabled,
        run: openStrategicContextTarget ? () => openContextTarget(mapTarget) : focusStrategicComposer,
      });
      const drilldowns = [
        ...(contextPackage.drilldowns ?? []),
        ...(contextPackage.canvas?.drilldowns ?? []),
      ];
      const seenDrilldownTargets = new Set<string>();
      drilldowns.slice(0, 12).forEach(drilldown => {
        const targetKey = JSON.stringify(drilldown.target ?? {});
        const key = `${drilldown.id}:${targetKey}`;
        if (seenDrilldownTargets.has(key)) return;
        seenDrilldownTargets.add(key);
        addContextCommand({
          id: `strategic.context.open.drilldown.${drilldown.id}`,
          label: `Open context: ${drilldown.label}`,
          section: 'Context',
          detail: 'context package',
          keywords: ['strategic', 'context', 'open', 'drilldown', drilldown.label],
          disabled: projectDisabled || !openStrategicContextTarget,
          run: () => openContextTarget(drilldown.target),
        });
      });
    }
    selectedRefs.slice(0, 12).forEach(ref => {
      addRefCommands({
        ref,
        label: fallbackSelectedRefLabel(ref),
        detail: 'kept',
        target: null,
        origin: 'kept',
      });
    });
    (strategicState?.work_session?.selected_ref_descriptors ?? []).slice(0, 8).forEach(ref => {
      addRefCommands({
        ref: selectedRefFromDescriptor(ref),
        label: ref.label,
        detail: ref.status ?? null,
        target: ref.target,
        origin: 'kept',
      });
    });
    canvasResourceRows.slice(0, 4).forEach(resource => {
      addRefCommands({
        ref: selectedRefFromResource(resource),
        label: resource.label,
        detail: resource.summary ?? canvasSummaryDetail(canvas, canvasNodeCount, canvasEdgeCount),
        target: resource.target,
        origin: 'map',
        allowOpen: false,
      });
    });
    listedResources.slice(0, 8).forEach(resource => {
      addRefCommands({
        ref: selectedRefFromResource(resource),
        label: resource.label,
        detail: resource.summary ?? null,
        target: resource.target,
        origin: 'resource',
      });
    });
    (contextPackage?.canvas?.nodes ?? [])
      .filter(node => node.kind !== 'project')
      .slice(0, 8)
      .forEach(node => {
        addRefCommands({
          ref: selectedRefFromNode(node),
          label: node.label,
          detail: compactContextDetail(node.summary, node.status),
          target: node.target,
          origin: 'map',
        });
      });
    (contextPackage?.evidence?.knowledge?.items ?? []).slice(0, 8).forEach(item => {
      addRefCommands({
        ref: selectedRefFromKnowledgeEvidence(item),
        label: item.label ?? item.topic ?? 'Knowledge document',
        detail: item.summary ?? item.source_kind ?? null,
        target: item.target,
        origin: 'evidence',
      });
    });
    (contextPackage?.evidence?.artifacts?.items ?? []).slice(0, 8).forEach(item => {
      addRefCommands({
        ref: selectedRefFromArtifactEvidence(item),
        label: item.label ?? item.logical_key ?? item.artifact_type ?? 'Artifact',
        detail: item.task_title ?? item.artifact_type ?? null,
        target: item.target,
        origin: 'evidence',
      });
    });
    const attentionCommandEntries = (strategicState?.attention?.items ?? [])
      .filter(item => !visibleEnabledWorkSessionActionIds.has(`attention.${item.id}`))
      .filter(item => !workSessionCommandIds.has(item.id) && !workSessionCommandIds.has(`attention.${item.id}`))
      .slice(0, 8)
      .map((item): WorkbenchCommand => ({
        id: `strategic.attention.${item.kind}.${item.id}`,
        label: attentionItemLabel(item),
        section: 'Attention',
        detail: compactContextDetail(item.reason, item.detail ?? item.status ?? item.severity) ?? undefined,
        keywords: ['strategic', 'attention', item.kind, item.reason, item.label, item.detail ?? '', item.status ?? ''],
        disabled: projectDisabled,
        run: () => openAttentionItem(item),
      }));
    const messageCommandEntries = (strategicState?.messages?.items ?? [])
      .filter(message => !coveredMessageIds.has(message.id))
      .slice(0, MESSAGE_COMMAND_LIMIT)
      .map((message): WorkbenchCommand => ({
        id: `strategic.messages.${messageCommandAction(message)}.${message.id}`,
        label: messageCommandLabel(message),
        section: 'Messages',
        detail: messageCommandDetail(message),
        keywords: [
          'strategic',
          'messages',
          'message',
          'reply',
          'question',
          'open',
          message.message_type,
          message.from_type,
          message.status,
          message.content,
        ],
        disabled: projectDisabled || !openStrategicMessageTarget,
        run: openStrategicMessageTarget
          ? () => runAndClose(() => openStrategicMessageTarget({
              messageId: message.id,
              action: messageCommandAction(message),
            }))
          : closePalette,
      }));
    const taskFocusEntries: StrategicStateTaskFocusItem[] = [];
    const seenTaskFocusIds = new Set<string>();
    ['needs_judgment', 'friction', 'active', 'queued', 'recent_completed'].forEach(bucket => {
      const items = strategicState?.tasks?.focus?.buckets[bucket]?.items ?? [];
      for (const item of items) {
        if (seenTaskFocusIds.has(item.id)) continue;
        seenTaskFocusIds.add(item.id);
        taskFocusEntries.push(item);
        if (taskFocusEntries.length >= TASK_FOCUS_COMMAND_LIMIT) return;
      }
    });
    const taskFocusCommandEntries = taskFocusEntries
      .slice(0, TASK_FOCUS_COMMAND_LIMIT)
      .map((item): WorkbenchCommand => ({
        id: `strategic.task_focus.${item.bucket}.${item.id}`,
        label: taskFocusCommandLabel(item),
        section: 'Tasks',
        detail: taskFocusCommandDetail(item),
        keywords: [
          'strategic',
          'task',
          'tasks',
          'task focus',
          'sideboard',
          'task context',
          item.bucket,
          item.status,
          item.task_type ?? '',
          item.title ?? '',
          item.detail ?? '',
          item.latest_status_message ?? '',
          item.blocked_reason ?? '',
        ],
        disabled: projectDisabled || !openStrategicContextTarget,
        run: () => openContextTarget({ surface: 'task', task_id: item.id }),
      }));
    const sessionMetricCommands = rankStrategicWorkSessionMetrics(
      strategicState?.work_session?.curated_metrics ?? [],
      10,
    )
      .map((metric): WorkbenchCommand => ({
        id: `strategic.session.metric.${metric.id}`,
        label: `Open ${metric.label}`,
        section: 'Session',
        detail: compactContextDetail(formatStrategicMetricValue(metric), metric.tone === 'neutral' ? null : metric.tone) ?? undefined,
        keywords: ['strategic', 'session', 'metric', metric.id, metric.label, metric.tone ?? '', metric.unit ?? ''],
        disabled: projectDisabled || !openStrategicContextTarget,
        run: () => openContextTarget(metric.target),
      }));
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
        label: 'Focus Strategic chat',
        section: 'Strategic',
        detail: selectedProjectRepoLabel ?? 'No project selected',
        keywords: ['chat', 'message', 'messages', 'steer', 'strategist'],
        shortcut: '1',
        disabled: projectDisabled,
        run: focusStrategicComposer,
      },
      ...workSessionDescriptorCommands,
      ...(markStrategicChecked && !workSessionCommandIds.has('session.check_in') ? [{
        id: 'strategic.check_in.mark',
        label: 'Mark Strategic checked',
        section: 'Strategic',
        detail: strategicCheckInDetail,
        keywords: ['check-in', 'check in', 'checked', 'daily', 'status', 'since last', 'strategic'],
        disabled: projectDisabled || !projectAccess.canOperateProject || checkingInStrategicSession,
        run: () => {
          if (!canOperateProjectRef.current) return;
          runAndClose(markStrategicChecked);
        },
      }] : []),
      ...(switchStrategicInteractionMode
        ? strategicModeCommands.filter(command => !workSessionCommandModes.has(command.mode)).map((command): WorkbenchCommand => ({
            id: command.id,
            label: command.label.replace('Switch Strategic to', 'Set Strategic mode:'),
            section: 'Strategic',
            detail: command.mode === strategicInteractionMode ? 'Current mode' : command.detail,
            keywords: ['mode', 'session', 'interaction', 'strategic', ...command.keywords],
            disabled: projectDisabled || !projectAccess.canOperateProject || savingStrategicWorkbench || command.mode === strategicInteractionMode,
            run: command.mode === strategicInteractionMode ? closePalette : () => setStrategicModeAndOpenChat(command.mode),
          }))
        : []),
      ...workSessionActionCommands,
      ...attentionCommandEntries,
      {
        id: 'strategic.messages.open',
        label: 'Open Strategic messages',
        section: 'Messages',
        detail: `${strategicState?.messages?.counts.open ?? 0} open, ${strategicState?.messages?.counts.linked ?? 0} linked`,
        keywords: ['strategic', 'messages', 'message', 'reply', 'question', 'feedback', 'blocker'],
        disabled: projectDisabled || !openStrategicMessageTarget,
        run: openStrategicMessageTarget ? () => runAndClose(() => openStrategicMessageTarget(undefined)) : closePalette,
      },
      ...messageCommandEntries,
      ...taskFocusCommandEntries,
      ...sessionMetricCommands,
      {
        id: 'strategic.context.workspace',
        label: 'Strategic context',
        section: 'Context',
        detail: contextPackage
          ? `${selectedRefs.length} kept, ${listedResources.length} resources, ${canvasNodeCount} nodes`
          : `${selectedRefs.length} kept`,
        keywords: ['strategic', 'context', 'workspace', 'drawer', 'canvas', 'map', 'evidence', 'kept'],
        disabled: projectDisabled || !openStrategicContextWorkspace,
        run: openStrategicContextWorkspace ? () => runAndClose(openStrategicContextWorkspace) : focusStrategicComposer,
      },
      ...contextCommandEntries,
      {
        id: 'workbench.refresh',
        label: 'Refresh workbench',
        section: 'Workbench',
        detail: 'Tasks + Strategic',
        keywords: ['reload', 'sync', 'status'],
        shortcut: 'R',
        run: () => runAndClose(refreshWorkbench),
      },
      {
        id: 'workbench.subtab.previous',
        label: 'Previous subtab',
        section: 'Workbench',
        detail: 'Current workspace section',
        keywords: ['previous', 'subtab', 'section', 'workspace'],
        shortcut: '[',
        disabled: !openPreviousWorkbenchSubtab,
        run: openPreviousWorkbenchSubtab ? () => runAndClose(openPreviousWorkbenchSubtab) : closePalette,
      },
      {
        id: 'workbench.subtab.next',
        label: 'Next subtab',
        section: 'Workbench',
        detail: 'Current workspace section',
        keywords: ['next', 'subtab', 'section', 'workspace'],
        shortcut: ']',
        disabled: !openNextWorkbenchSubtab,
        run: openNextWorkbenchSubtab ? () => runAndClose(openNextWorkbenchSubtab) : closePalette,
      },
      {
        id: 'workbench.shortcuts.open',
        label: 'Keyboard shortcuts',
        section: 'Workbench',
        detail: 'Command palette',
        keywords: ['keyboard', 'shortcuts', 'commands', 'help'],
        shortcut: '?',
        run: () => setCommandPaletteOpen(true),
      },
      {
        id: 'pipeline.flow',
        label: 'Pipeline tasks',
        section: 'Pipeline',
        detail: `${visibleActiveCount} in progress, ${visibleQueuedCount} queued`,
        keywords: ['tasks', 'flow', 'monitor', 'passive', 'pipeline'],
        shortcut: '2',
        run: () => openPipelineSection('flow'),
      },
      {
        id: 'pipeline.board',
        label: 'Pipeline board',
        section: 'Pipeline',
        detail: `${filteredTaskCount} visible tasks`,
        keywords: ['kanban', 'tasks', 'board'],
        run: () => openPipelineSection('board'),
      },
      {
        id: 'pipeline.archive',
        label: 'Pipeline archive',
        section: 'Pipeline',
        detail: 'Terminal work',
        keywords: ['archived', 'terminal'],
        shortcut: 'A',
        run: () => openPipelineSection('archive'),
      },
      {
        id: 'project.update',
        label: 'Project running update',
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
        id: 'project.autonomy.toggle',
        label: 'Toggle autonomous mode',
        section: 'Project',
        detail: autonomousEnabled ? 'On — turn off' : 'Off — turn on',
        keywords: ['autonomous', 'autonomy', 'auto', 'pause', 'resume', 'enable', 'disable', 'toggle', 'mode'],
        disabled: projectDisabled || !projectAccess.canManageProject || !toggleAutonomousMode,
        run: toggleAutonomousMode
          ? () => runAndClose(toggleAutonomousMode)
          : closePalette,
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
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openProjectManageSection('overview'),
      },
      {
        id: 'project.build',
        label: 'Project build settings',
        section: 'Project',
        detail: 'Detection, test, install commands',
        keywords: ['settings', 'build', 'test', 'install', 'commands'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openSettingsSectionHome('build-config'),
      },
      {
        id: 'project.branches',
        label: 'Project branches',
        section: 'Project',
        detail: 'Integration and promotion targets',
        keywords: ['settings', 'branches', 'integration', 'promotion'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openSettingsSectionHome('branches'),
      },
      {
        id: 'project.review',
        label: 'Project auto-review',
        section: 'Project',
        detail: 'Review gates and autonomous decisions',
        keywords: ['settings', 'review', 'auto-review', 'approval', 'gates'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openSettingsSectionHome('auto-review'),
      },
      {
        id: 'project.resources',
        label: 'Project resources',
        section: 'Project',
        detail: 'Runtime and budget envelope',
        keywords: ['settings', 'resources', 'budget', 'runtime', 'envelope'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openSettingsSectionHome('resource-envelope'),
      },
      {
        id: 'project.remediation',
        label: 'Project remediation',
        section: 'Project',
        detail: 'Failure recovery behavior',
        keywords: ['settings', 'remediation', 'recovery', 'failure', 'auto-remediation'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openSettingsSectionHome('auto-remediation'),
      },
      {
        id: 'project.issue_implementation',
        label: 'Project issue implementation',
        section: 'Project',
        detail: 'Issue implementation policy',
        keywords: ['settings', 'issues', 'implementation', 'github', 'policy'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openSettingsSectionHome('issue-implementation'),
      },
      {
        id: 'project.alert_domains',
        label: 'Project alert domains',
        section: 'Project',
        detail: 'Project alert routing',
        keywords: ['settings', 'alerts', 'domains', 'routing', 'notifications'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openSettingsSectionHome('alert-domains'),
      },
      {
        id: 'project.access',
        label: 'Project access settings',
        section: 'Project',
        detail: 'Roles and project access policy',
        keywords: ['settings', 'access', 'members', 'roles', 'users'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => openSettingsSectionHome('access'),
      },
      {
        id: 'project.members',
        label: 'Project members',
        section: 'Project',
        detail: 'Access',
        keywords: ['access', 'team', 'users'],
        disabled: projectDisabled || !projectAccess.canManageProject,
        run: () => runAndClose(openProjectMembers),
      },
      {
        id: 'project.switcher',
        label: 'Manage projects',
        section: 'Project',
        detail: 'Create, archive, restore',
        keywords: ['project management', 'archive', 'delete'],
        run: () => runAndClose(openProjectManagement),
      },
      {
        id: 'runtime.health',
        label: 'Runtime overview',
        section: 'Runtime',
        detail: apiHealthy ? 'API healthy' : 'API degraded',
        keywords: ['overview', 'health', 'devices', 'runner', 'orchestrator', 'accounts', 'settings'],
        shortcut: '4',
        run: () => openRuntimeSection('health'),
      },
      {
        id: 'runtime.analytics',
        label: 'Runtime analytics',
        section: 'Runtime',
        detail: 'Cost and portfolio metrics',
        keywords: ['analytics', 'dashboard', 'metrics', 'cost', 'portfolio'],
        run: () => openRuntimeSection('analytics'),
      },
      {
        id: 'runtime.devices',
        label: 'Runtime devices',
        section: 'Runtime',
        detail: 'Warm VMs and runners',
        keywords: ['devices', 'runner', 'local', 'hardware', 'vm', 'warm'],
        run: () => openRuntimeSection('devices'),
      },
      {
        id: 'runtime.accounts',
        label: 'Runtime accounts',
        section: 'Runtime',
        detail: `${maxPlanAccountCount} LLM accounts`,
        keywords: ['accounts', 'cost', 'spend', 'models', 'usage', 'caps', 'pool', 'github', 'llm'],
        run: () => openSettingsSectionHome('accounts'),
      },
      {
        id: 'runtime.compute',
        label: 'Runtime compute',
        section: 'Runtime',
        detail: 'Jobs and held approvals',
        keywords: ['compute', 'jobs', 'experiments', 'approvals', 'queue'],
        run: () => openRuntimeSection('compute'),
      },
      {
        id: 'runtime.settings',
        label: 'Runtime orchestrator settings',
        section: 'Runtime',
        detail: 'Orchestrator defaults',
        keywords: ['settings', 'runtime', 'orchestrator', 'provider', 'routing', 'configuration'],
        run: () => openSettingsSectionHome('orchestrator'),
      },
      {
        id: 'runtime.settings.general',
        label: 'Runtime settings',
        section: 'Runtime',
        detail: 'Defaults, refresh, and local UI behavior',
        keywords: ['settings', 'runtime', 'general', 'defaults', 'refresh', 'local'],
        shortcut: 'S',
        run: () => openSettingsSectionHome('general'),
      },
      {
        id: 'runtime.settings.deploy',
        label: 'Runtime deploy settings',
        section: 'Runtime',
        detail: 'Pipeline health and deploy controls',
        keywords: ['settings', 'runtime', 'deploy', 'deployment', 'health'],
        disabled: !isAdmin,
        run: () => openSettingsSectionHome('deploy'),
      },
      {
        id: 'runtime.settings.provider-routing',
        label: 'Provider routing',
        section: 'Runtime',
        detail: 'Task and category policy',
        keywords: ['settings', 'provider', 'routing', 'models', 'policy'],
        disabled: !isAdmin,
        run: () => openSettingsSectionHome('provider-routing'),
      },
      {
        id: 'runtime.settings.provider-settings',
        label: 'Provider settings',
        section: 'Runtime',
        detail: 'Model defaults and effort routing',
        keywords: ['settings', 'provider', 'models', 'effort', 'defaults'],
        disabled: !isAdmin,
        run: () => openSettingsSectionHome('provider-settings'),
      },
      {
        id: 'runtime.settings.notifications',
        label: 'Runtime notifications',
        section: 'Runtime',
        detail: 'Webhook, event, and spend alerts',
        keywords: ['settings', 'notifications', 'alerts', 'webhook', 'spend'],
        run: () => openSettingsSectionHome('notifications'),
      },
      {
        id: 'runtime.settings.advanced',
        label: 'Runtime advanced settings',
        section: 'Runtime',
        detail: 'Health checks and diagnostics',
        keywords: ['settings', 'advanced', 'health checks', 'diagnostics'],
        run: () => openSettingsSectionHome('advanced'),
      },
      {
        id: 'runtime.version-lifecycle',
        label: 'Version lifecycle',
        section: 'Runtime',
        detail: 'Qualification status, promotion controls, Run Qualify',
        keywords: ['version', 'lifecycle', 'qualify', 'promotion', 'release', 'candidate', 'sha', 'run qualify'],
        disabled: projectDisabled || !hasVersionLifecycle,
        run: () => openRuntimeSection('version-lifecycle'),
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
        disabled: projectDisabled || !projectAccess.canOperateProject,
        run: () => {
          if (!canOperateProjectRef.current) return;
          openNewTaskModal();
          closePalette();
        },
      },
    ].filter(command => (
      isAdmin
      || (
        command.id !== 'runtime.analytics'
        && command.id !== 'runtime.accounts'
        && !command.id.startsWith('runtime.settings')
      )
    ));
  }, [
    apiHealthy,
    autonomousEnabled,
    toggleAutonomousMode,
    designRevision,
    directionsCount,
    filteredTaskCount,
    focusStrategicComposer,
    handleTabChange,
    hasDesignDoc,
    hasVersionLifecycle,
    isAdmin,
    maxPlanAccountCount,
    openStrategicContextWorkspace,
    openStrategicContextTarget,
    openStrategicMessageTarget,
    openNewTaskModal,
    openProjectManagement,
    openProjectMembers,
    openPreviousWorkbenchSubtab,
    openNextWorkbenchSubtab,
    problemModelRevision,
    projectAccess.canManageProject,
    projectAccess.canOperateProject,
    refreshSettings,
    refreshWorkbench,
    savingStrategicWorkbench,
    checkingInStrategicSession,
    selectedProjectId,
    selectedProjectRepoLabel,
    setCommandPaletteOpen,
    setPipelineSection,
    setProjectManageSection,
    setProjectSection,
    setRuntimeConfigSection,
    setRuntimeSection,
    setShowActivityFeed,
    setShowArchive,
    setShowKnowledgeBrowser,
    setShowResearchBrowser,
    strategicState,
    strategicWorkbenchState,
    switchStrategicInteractionMode,
    markStrategicChecked,
    setViewMode,
    strategicInteractionMode,
    updateStrategicSessionRefs,
    visibleActiveCount,
    visibleQueuedCount,
    workSessionCommandStatuses,
  ]);
}
