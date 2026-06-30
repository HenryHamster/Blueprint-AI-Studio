import { useMemo } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import type { StrategicInteractionMode } from '../components/StrategicSessionControl';
import type { SectionId } from '../components/SettingsDropdown';
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
  StrategicWorkbenchState,
  StrategicWorkSessionAction,
  StrategicWorkSessionRefDescriptor,
} from '../strategicStateApi';
import { formatStrategicMetricValue, rankStrategicWorkSessionMetrics } from '../utils/strategicWorkSessionMetrics';
import { removeStrategicSelectedRef, upsertStrategicSelectedRef, type StrategicSelectedRef } from '../utils/strategicWorkbenchSession';
import type { PrimaryTab } from './useUIState';
import type { ViewMode } from './useViewPreferences';

export type PipelineSection = 'flow' | 'board' | 'archive';
type StrategicContextTarget = StrategicContextPackageDrilldown['target'];
type StrategicContextCanvas = NonNullable<StrategicContextPackageDescriptor['canvas']>;
const WORK_SESSION_ACTION_COMMAND_LIMIT = 4;

interface UseWorkbenchCommandsOptions {
  selectedProjectId: string | null;
  selectedProjectRepoLabel: string | null;
  directionsCount: number;
  filteredTaskCount: number;
  visibleActiveCount: number;
  visibleQueuedCount: number;
  maxPlanAccountCount: number;
  apiHealthy: boolean;
  isAdmin?: boolean;
  problemModelRevision?: number | null;
  hasDesignDoc?: boolean;
  designRevision?: number | null;
  strategicInteractionMode: StrategicInteractionMode;
  strategicState?: StrategicStateResponse | null;
  strategicWorkbenchState?: StrategicWorkbenchState | null;
  savingStrategicWorkbench?: boolean;
  switchStrategicInteractionMode: (mode: StrategicInteractionMode) => void;
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
  setProjectManageSection?: (section: ProjectManageSection) => void;
  setRuntimeSection: (section: RuntimeSection) => void;
  setRuntimeConfigSection: (section: RuntimeSettingsSection) => void;
  setViewMode: (mode: ViewMode) => void;
  setShowArchive: (show: boolean) => void;
  setShowActivityFeed: (show: boolean) => void;
  setShowResearchBrowser: (show: boolean) => void;
  setShowKnowledgeBrowser: (show: boolean) => void;
  openNewTaskModal: () => void;
  openGlobalSettings: (section: SectionId | null) => void;
  closeGlobalSettings?: () => void;
  refreshWorkbench: () => void;
  openProjectManagement: () => void;
  openProjectMembers: () => void;
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
  return Boolean(workSessionActionMessageId(action) || action.target?.surface === 'messages');
}

function workSessionActionContextTarget(action: StrategicWorkSessionAction): StrategicContextTarget | null {
  const target = action.target ?? null;
  if (!target) return null;
  if (target.surface) return target;
  if (target.task_id) return { ...target, surface: 'task' };
  return null;
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
  isAdmin = true,
  problemModelRevision,
  hasDesignDoc,
  designRevision,
  strategicInteractionMode,
  strategicState,
  strategicWorkbenchState,
  savingStrategicWorkbench = false,
  switchStrategicInteractionMode,
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
  openGlobalSettings,
  closeGlobalSettings,
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
    const openProjectManageSection = (section: ProjectManageSection) => {
      setProjectSection('manage');
      setProjectManageSection?.(section);
      handleTabChange('project');
      if (setProjectManageSection) {
        closeGlobalSettings?.();
      } else if (section !== 'overview') {
        openGlobalSettings(section);
      }
      closePalette();
    };
    const openRuntimeSection = (section: RuntimeSection) => {
      setRuntimeSection(section);
      handleTabChange('runtime');
      closeGlobalSettings?.();
      closePalette();
    };
    const openRuntimeSettingsSection = (section: RuntimeSettingsSection) => {
      setRuntimeSection('settings');
      setRuntimeConfigSection(section);
      handleTabChange('runtime');
      closeGlobalSettings?.();
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
      openStrategicContextTarget?.(target);
      closePalette();
    };
    const openAttentionItem = (item: StrategicStateAttentionItem) => {
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
    const visibleWorkSessionActions = (strategicState?.work_session?.actions ?? []).slice(0, WORK_SESSION_ACTION_COMMAND_LIMIT);
    const visibleEnabledWorkSessionActionIds = new Set(
      visibleWorkSessionActions.filter(action => action.enabled).map(action => action.id),
    );
    const workSessionActionCanRoute = (action: StrategicWorkSessionAction): boolean => {
      if (workSessionActionIsMessageTarget(action)) return Boolean(openStrategicMessageTarget || openStrategicContextTarget);
      return Boolean(openStrategicContextTarget && workSessionActionContextTarget(action));
    };
    const openWorkSessionAction = (action: StrategicWorkSessionAction) => {
      if (!action.enabled) {
        closePalette();
        return;
      }

      const target = action.target ?? null;
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
      updateStrategicSessionRefs?.(refs => upsertStrategicSelectedRef(refs, ref));
      closePalette();
    };
    const removeContextRef = (ref: Pick<StrategicSelectedRef, 'kind' | 'id'>) => {
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
          disabled: projectDisabled || savingStrategicWorkbench,
          run: selected ? () => removeContextRef(source.ref) : () => keepContextRef(source.ref),
        });
      }
    };
    if (contextPackage) {
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
      ...workSessionActionCommands,
      ...attentionCommandEntries,
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
        label: 'Refresh',
        section: 'Workbench',
        detail: 'Tasks + Strategic',
        keywords: ['reload', 'sync', 'status'],
        shortcut: 'R',
        run: () => runAndClose(refreshWorkbench),
      },
      {
        id: 'pipeline.flow',
        label: 'Pipeline tasks',
        section: 'Pipeline',
        detail: `${visibleActiveCount} active, ${visibleQueuedCount} queued`,
        keywords: ['tasks', 'flow', 'monitor', 'passive', 'pipeline'],
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
        run: () => openProjectManageSection('overview'),
      },
      {
        id: 'project.build',
        label: 'Project build settings',
        section: 'Project',
        detail: 'Detection, test, install commands',
        keywords: ['settings', 'build', 'test', 'install', 'commands'],
        disabled: projectDisabled,
        run: () => openProjectManageSection('build-config'),
      },
      {
        id: 'project.branches',
        label: 'Project branches',
        section: 'Project',
        detail: 'Integration and promotion targets',
        keywords: ['settings', 'branches', 'integration', 'promotion'],
        disabled: projectDisabled,
        run: () => openProjectManageSection('branches'),
      },
      {
        id: 'project.review',
        label: 'Project auto-review',
        section: 'Project',
        detail: 'Review gates and autonomous decisions',
        keywords: ['settings', 'review', 'auto-review', 'approval', 'gates'],
        disabled: projectDisabled,
        run: () => openProjectManageSection('auto-review'),
      },
      {
        id: 'project.resources',
        label: 'Project resources',
        section: 'Project',
        detail: 'Runtime and budget envelope',
        keywords: ['settings', 'resources', 'budget', 'runtime', 'envelope'],
        disabled: projectDisabled,
        run: () => openProjectManageSection('resource-envelope'),
      },
      {
        id: 'project.access',
        label: 'Project access settings',
        section: 'Project',
        detail: 'Roles and project access policy',
        keywords: ['settings', 'access', 'members', 'roles', 'users'],
        disabled: projectDisabled,
        run: () => openProjectManageSection('access'),
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
        run: () => openRuntimeSection('accounts'),
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
        label: 'Runtime settings',
        section: 'Runtime',
        detail: 'Orchestrator defaults',
        keywords: ['settings', 'runtime', 'orchestrator', 'provider', 'routing', 'configuration'],
        run: () => openRuntimeSettingsSection('orchestrator'),
      },
      {
        id: 'runtime.settings.provider-routing',
        label: 'Provider routing',
        section: 'Runtime',
        detail: 'Task and category policy',
        keywords: ['settings', 'provider', 'routing', 'models', 'policy'],
        disabled: !isAdmin,
        run: () => openRuntimeSettingsSection('provider-routing'),
      },
      {
        id: 'runtime.settings.provider-settings',
        label: 'Provider settings',
        section: 'Runtime',
        detail: 'Model defaults and effort routing',
        keywords: ['settings', 'provider', 'models', 'effort', 'defaults'],
        disabled: !isAdmin,
        run: () => openRuntimeSettingsSection('provider-settings'),
      },
      {
        id: 'runtime.settings.notifications',
        label: 'Runtime notifications',
        section: 'Runtime',
        detail: 'Webhook, event, and spend alerts',
        keywords: ['settings', 'notifications', 'alerts', 'webhook', 'spend'],
        run: () => openRuntimeSettingsSection('notifications'),
      },
      {
        id: 'runtime.settings.advanced',
        label: 'Runtime advanced settings',
        section: 'Runtime',
        detail: 'Health checks and diagnostics',
        keywords: ['settings', 'advanced', 'health checks', 'diagnostics'],
        run: () => openRuntimeSettingsSection('advanced'),
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
        label: 'Workbench settings',
        section: 'Workbench',
        detail: 'UI defaults and controls',
        keywords: ['settings', 'preferences', 'config', 'workbench'],
        shortcut: 'S',
        run: () => runAndClose(() => openGlobalSettings('general')),
      },
    ];
  }, [
    apiHealthy,
    closeGlobalSettings,
    designRevision,
    directionsCount,
    filteredTaskCount,
    focusStrategicComposer,
    handleTabChange,
    hasDesignDoc,
    isAdmin,
    maxPlanAccountCount,
    openStrategicContextWorkspace,
    openStrategicContextTarget,
    openStrategicMessageTarget,
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
    setViewMode,
    strategicInteractionMode,
    updateStrategicSessionRefs,
    visibleActiveCount,
    visibleQueuedCount,
  ]);
}
