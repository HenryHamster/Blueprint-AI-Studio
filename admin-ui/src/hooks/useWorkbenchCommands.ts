import { useMemo } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import type { ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import type { StrategicInteractionMode } from '../components/StrategicSessionControl';
import type { SectionId } from '../components/SettingsDropdown';
import type {
  StrategicContextArtifactEvidence,
  StrategicContextCanvasNode,
  StrategicContextKnowledgeEvidence,
  StrategicContextPackageDrilldown,
  StrategicContextResourceRef,
  StrategicMessageTarget,
  StrategicStateResponse,
  StrategicStateAttentionItem,
  StrategicWorkbenchState,
  StrategicWorkSessionMetric,
  StrategicWorkSessionRefDescriptor,
} from '../strategicStateApi';
import { removeStrategicSelectedRef, upsertStrategicSelectedRef, type StrategicSelectedRef } from '../utils/strategicWorkbenchSession';
import type { PrimaryTab } from './useUIState';
import type { ViewMode } from './useViewPreferences';

export type PipelineSection = 'flow' | 'board' | 'archive';
type StrategicContextTarget = StrategicContextPackageDrilldown['target'];

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

function contextKindLabel(kind?: string | null): string {
  if (kind === 'knowledge_document') return 'knowledge';
  if (kind === 'canvas') return 'map';
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

function attentionItemLabel(item: StrategicStateAttentionItem): string {
  const label = item.label?.trim() || item.detail?.trim() || 'attention item';
  if (item.kind === 'message' && item.reason === 'question') return `Reply to question: ${label}`;
  if (item.kind === 'message') return `Open message: ${label}`;
  if (item.kind === 'task') return `Open attention task: ${label}`;
  if (item.kind === 'direction') return `Open direction: ${label}`;
  if (item.kind === 'alert') return `Open alert: ${label}`;
  return `Open attention: ${label}`;
}

function metricValueDetail(metric: StrategicWorkSessionMetric): string {
  const raw = metric.value;
  const value = typeof raw === 'number'
    ? (Number.isInteger(raw) ? String(raw) : raw.toFixed(2))
    : String(raw ?? '');
  const unit = metric.unit?.trim();
  return unit ? `${value} ${unit}` : value;
}

function metricIsActionable(metric: StrategicWorkSessionMetric): boolean {
  return Boolean(metric.target) && (Number(metric.value) > 0 || (metric.tone != null && metric.tone !== 'neutral'));
}

function metricPriority(metric: StrategicWorkSessionMetric): number {
  if (metric.tone === 'critical') return 50;
  if (metric.tone === 'warning') return 40;
  if (metric.tone === 'active') return 30;
  if (metric.tone === 'watch') return 20;
  if (Number(metric.value) > 0) return 10;
  return 0;
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
    const selectedRefs = strategicWorkbenchState?.selected_refs ?? strategicState?.work_session?.selected_refs ?? strategicState?.workbench?.selected_refs ?? [];
    const selectedKeys = new Set(selectedRefs.map(contextRefKey));
    const contextPackage = strategicState?.context_package ?? null;
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
    }) => {
      const key = contextRefKey(source.ref);
      const selected = selectedKeys.has(key);
      const detail = compactContextDetail(contextKindLabel(source.ref.kind), source.detail) ?? contextKindLabel(source.ref.kind);
      if (source.target && openStrategicContextTarget && !seenContextOpenRefs.has(key)) {
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
      const resourceCount = contextPackage.resources?.length ?? 0;
      const nodeCount = contextPackage.canvas?.node_count ?? contextPackage.canvas?.nodes?.length ?? 0;
      const mapTarget = contextPackage.resources?.find(resource => resource.kind === 'canvas')?.target
        ?? { surface: 'project', section: 'problem-model' };
      addContextCommand({
        id: 'strategic.context.map',
        label: 'Strategic context map',
        section: 'Context',
        detail: `${selectedRefs.length} kept, ${resourceCount} resources, ${nodeCount} nodes`,
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
    (contextPackage?.resources ?? []).slice(0, 8).forEach(resource => {
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
    const sessionMetricCommands = (strategicState?.work_session?.curated_metrics ?? [])
      .filter(metricIsActionable)
      .sort((a, b) => metricPriority(b) - metricPriority(a) || Number(b.value) - Number(a.value))
      .slice(0, 10)
      .map((metric): WorkbenchCommand => ({
        id: `strategic.session.metric.${metric.id}`,
        label: `Open ${metric.label}`,
        section: 'Session',
        detail: compactContextDetail(metricValueDetail(metric), metric.tone === 'neutral' ? null : metric.tone) ?? undefined,
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
      ...attentionCommandEntries,
      ...sessionMetricCommands,
      {
        id: 'strategic.context.workspace',
        label: 'Strategic context',
        section: 'Context',
        detail: contextPackage
          ? `${selectedRefs.length} kept, ${contextPackage.resources?.length ?? 0} resources, ${contextPackage.canvas?.node_count ?? contextPackage.canvas?.nodes?.length ?? 0} nodes`
          : `${selectedRefs.length} kept`,
        keywords: ['strategic', 'context', 'workspace', 'drawer', 'canvas', 'evidence', 'kept'],
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
        id: 'runtime.analytics',
        label: 'Runtime analytics',
        section: 'Runtime',
        detail: 'Dashboard metrics',
        keywords: ['analytics', 'dashboard', 'metrics'],
        run: () => openRuntimeSection('analytics'),
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
