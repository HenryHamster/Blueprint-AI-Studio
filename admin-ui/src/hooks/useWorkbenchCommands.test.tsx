import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useEffect } from 'react';
import { useWorkbenchCommands, type PipelineSection } from './useWorkbenchCommands';
import type { ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import type { StrategicInteractionMode } from '../components/StrategicSessionControl';
import type { StrategicStateResponse, StrategicWorkbenchState, StrategicWorkSessionAction } from '../strategicStateApi';
import type { ViewMode } from './useViewPreferences';

type Captured = ReturnType<typeof useWorkbenchCommands>;
let captured: Captured = [];

function Harness(overrides: Partial<Parameters<typeof useWorkbenchCommands>[0]> = {}) {
  const commands = useWorkbenchCommands({
    selectedProjectId: 'project-1',
    selectedProjectRepoLabel: 'owner/repo',
    directionsCount: 2,
    filteredTaskCount: 12,
    visibleActiveCount: 3,
    visibleQueuedCount: 4,
    maxPlanAccountCount: 5,
    apiHealthy: true,
    problemModelRevision: 7,
    hasDesignDoc: true,
    designRevision: 8,
    strategicInteractionMode: 'chat',
    savingStrategicWorkbench: false,
    switchStrategicInteractionMode: vi.fn(),
    focusStrategicComposer: vi.fn(),
    handleTabChange: vi.fn(),
    setCommandPaletteOpen: vi.fn(),
    setPipelineSection: vi.fn(),
    setProjectSection: vi.fn(),
    setRuntimeSection: vi.fn(),
    setRuntimeConfigSection: vi.fn(),
    setViewMode: vi.fn(),
    setShowArchive: vi.fn(),
    setShowActivityFeed: vi.fn(),
    setShowResearchBrowser: vi.fn(),
    setShowKnowledgeBrowser: vi.fn(),
    openNewTaskModal: vi.fn(),
    openGlobalSettings: vi.fn(),
    refreshWorkbench: vi.fn(),
    openProjectManagement: vi.fn(),
    openProjectMembers: vi.fn(),
    ...overrides,
  });
  useEffect(() => {
    captured = commands;
  }, [commands]);
  return null;
}

describe('useWorkbenchCommands', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    host?.remove();
    host = null;
    captured = [];
  });

  function mount(overrides: Partial<Parameters<typeof useWorkbenchCommands>[0]> = {}) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    act(() => {
      currentRoot.render(<Harness {...overrides} />);
    });
  }

  test('routes Strategic and project commands through existing workbench surfaces', () => {
    const focusStrategicComposer = vi.fn();
    const handleTabChange = vi.fn();
    const setProjectSection = vi.fn<(section: ProjectSection) => void>();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    mount({ focusStrategicComposer, handleTabChange, setProjectSection, setCommandPaletteOpen });

    captured.find(command => command.id === 'strategic.focus')?.run();
    captured.find(command => command.id === 'project.model')?.run();

    expect(focusStrategicComposer).toHaveBeenCalledTimes(1);
    expect(setProjectSection).toHaveBeenCalledWith('problem-model');
    expect(handleTabChange).toHaveBeenCalledWith('project');
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('routes Strategic mode commands through the shared workbench session setter', () => {
    const switchStrategicInteractionMode = vi.fn<(mode: StrategicInteractionMode) => void>();
    const handleTabChange = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    mount({
      strategicInteractionMode: 'continuous',
      switchStrategicInteractionMode,
      handleTabChange,
      setCommandPaletteOpen,
    });

    expect(captured.filter(command => command.id.startsWith('strategic.mode.')).map(command => command.id)).toEqual([
      'strategic.mode.chat',
      'strategic.mode.check_in',
      'strategic.mode.monitor',
      'strategic.mode.continuous',
    ]);
    const currentModeCommand = captured.find(command => command.id === 'strategic.mode.continuous');
    expect(currentModeCommand?.detail).toBe('Current mode');
    expect(currentModeCommand?.disabled).toBe(true);
    currentModeCommand?.run();

    captured.find(command => command.id === 'strategic.mode.monitor')?.run();

    expect(switchStrategicInteractionMode).toHaveBeenCalledWith('monitor');
    expect(switchStrategicInteractionMode).toHaveBeenCalledTimes(1);
    expect(handleTabChange).not.toHaveBeenCalledWith('chat');
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('exposes dynamic Strategic context commands for open, keep, and remove actions', () => {
    const target = { surface: 'task', task_id: 'task-context-1' } as const;
    const openStrategicContextTarget = vi.fn();
    const openStrategicContextWorkspace = vi.fn();
    const updateStrategicSessionRefs = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const strategicState = {
      work_session: {
        selected_refs: [
          { kind: 'artifact', id: 'artifact:latest', label: 'Latest report' },
        ],
        selected_ref_descriptors: [
          {
            kind: 'artifact',
            id: 'artifact:latest',
            label: 'Latest report',
            status: 'active',
            target,
          },
        ],
      },
      context_package: {
        resources: [
          {
            kind: 'canvas',
            id: 'strategic-canvas:project-1',
            label: 'Strategic context map',
            summary: '9 nodes / 12 links',
            target: { surface: 'project', section: 'problem-model' },
          },
          {
            kind: 'artifact',
            id: 'artifact:latest',
            label: 'Latest report',
            summary: 'Duplicate resource should not add another command',
            target,
          },
          {
            kind: 'knowledge_document',
            id: 'knowledge:operator-notes',
            label: 'Operator notes',
            summary: 'Current workday notes',
            target: { surface: 'knowledge', document_id: 'knowledge:operator-notes' },
          },
        ],
        canvas: {
          node_count: 1,
          nodes: [
            {
              id: 'task-context-1',
              kind: 'task',
              label: 'Investigate failed runner',
              summary: 'Runner queue is stalled',
              status: 'blocked',
              target,
            },
          ],
        },
        drilldowns: [
          {
            id: 'runtime',
            label: 'Runtime',
            target: { surface: 'runtime', section: 'compute' },
          },
        ],
        evidence: {
          knowledge: {
            total: 1,
            items: [
              {
                id: 'operator-notes-deep',
                kind: 'knowledge_document',
                label: 'Detailed operator notes',
                summary: 'Not included in the short resource list',
                target: { surface: 'knowledge', document_id: 'operator-notes-deep' },
              },
            ],
          },
          artifacts: {
            total: 1,
            items: [
              {
                id: 'latest-pr',
                kind: 'pull_request',
                label: 'Latest pull request',
                task_title: 'Validate context command pass',
                target: { surface: 'task', task_id: 'task-context-2', artifact_id: 'latest-pr' },
              },
            ],
          },
        },
      },
    } as StrategicStateResponse;
    const strategicWorkbenchState = {
      version: 1,
      interaction_mode: 'continuous',
      active_surface: 'strategic',
      selected_refs: [
        { kind: 'artifact', id: 'artifact:latest', label: 'Latest report' },
        { kind: 'task', id: 'task:optimistic', label: 'Optimistic task' },
      ],
    } as StrategicWorkbenchState;

    mount({
      strategicState,
      strategicWorkbenchState,
      openStrategicContextTarget,
      openStrategicContextWorkspace,
      updateStrategicSessionRefs,
      setCommandPaletteOpen,
    });

    captured.find(command => command.id === 'strategic.context.workspace')?.run();
    expect(openStrategicContextWorkspace).toHaveBeenCalledTimes(1);

    expect(captured.filter(command => command.label.includes('Latest report'))).toHaveLength(2);
    expect(captured.filter(command => command.id === 'strategic.context.remove.artifact:artifact:latest')).toHaveLength(1);
    expect(captured.filter(command => command.id === 'strategic.context.open.kept.artifact:artifact:latest')).toHaveLength(1);
    expect(captured.find(command => command.id === 'strategic.context.keep.task:task-context-1')?.detail).toBe('task / Runner queue is stalled / blocked');

    captured.find(command => command.id === 'strategic.context.map')?.run();
    expect(openStrategicContextTarget).toHaveBeenCalledWith({ surface: 'project', section: 'problem-model' });

    captured.find(command => command.id === 'strategic.context.open.drilldown.runtime')?.run();
    expect(openStrategicContextTarget).toHaveBeenCalledWith({ surface: 'runtime', section: 'compute' });

    captured.find(command => command.id === 'strategic.context.open.kept.artifact:artifact:latest')?.run();
    expect(openStrategicContextTarget).toHaveBeenCalledWith(target);

    captured.find(command => command.id === 'strategic.context.keep.knowledge_document:knowledge:operator-notes')?.run();
    expect(updateStrategicSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    let updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
    expect(updater([])).toEqual([
      { kind: 'knowledge_document', id: 'knowledge:operator-notes', label: 'Operator notes' },
    ]);

    captured.find(command => command.id === 'strategic.context.keep.knowledge_document:knowledge:operator-notes-deep')?.run();
    expect(updateStrategicSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
    expect(updater([])).toEqual([
      { kind: 'knowledge_document', id: 'knowledge:operator-notes-deep', label: 'Detailed operator notes' },
    ]);

    captured.find(command => command.id === 'strategic.context.open.evidence.artifact:artifact:latest-pr')?.run();
    expect(openStrategicContextTarget).toHaveBeenCalledWith({ surface: 'task', task_id: 'task-context-2', artifact_id: 'latest-pr' });

    captured.find(command => command.id === 'strategic.context.remove.artifact:artifact:latest')?.run();
    expect(updateStrategicSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
    expect(updater([{ kind: 'artifact', id: 'artifact:latest', label: 'Latest report' }])).toEqual([]);

    captured.find(command => command.id === 'strategic.context.remove.task:task:optimistic')?.run();
    expect(updateStrategicSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
    expect(updater([{ kind: 'task', id: 'task:optimistic', label: 'Optimistic task' }])).toEqual([]);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('exposes attention items and actionable session metrics as commands', () => {
    const openStrategicContextTarget = vi.fn();
    const openStrategicMessageTarget = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const strategicState = {
      attention: {
        items: [
          {
            id: 'attention-msg-1',
            kind: 'message',
            severity: 'needs_operator',
            reason: 'question',
            label: 'Choose a runner',
            detail: 'Strategic needs a routing choice',
            ref: { kind: 'message', id: 'msg-1', project_id: 'project-1' },
          },
          {
            id: 'attention-task-1',
            kind: 'task',
            severity: 'watch',
            reason: 'blocked',
            label: 'Fix worker bootstrap',
            ref: { kind: 'task', id: 'task-1', project_id: 'project-1' },
          },
        ],
      },
      work_session: {
        curated_metrics: [
          ...Array.from({ length: 11 }, (_, index) => ({
            id: `routine.${index}`,
            label: `routine ${index}`,
            value: 1,
            tone: 'neutral',
            target: { surface: 'project', section: 'running-update' },
          })),
          {
            id: 'messages.open',
            label: 'open messages',
            value: 0,
            tone: 'neutral',
            target: { surface: 'messages' },
          },
          {
            id: 'runtime.compute_failed',
            label: 'failed compute',
            value: 2,
            tone: 'critical',
            target: { surface: 'runtime', section: 'compute' },
          },
        ],
      },
    } as StrategicStateResponse;

    mount({ strategicState, openStrategicContextTarget, openStrategicMessageTarget, setCommandPaletteOpen });

    const questionCommand = captured.find(command => command.id === 'strategic.attention.message.attention-msg-1');
    expect(questionCommand?.label).toBe('Reply to question: Choose a runner');
    questionCommand?.run();
    expect(openStrategicMessageTarget).toHaveBeenCalledWith({ messageId: 'msg-1', action: 'reply' });

    captured.find(command => command.id === 'strategic.attention.task.attention-task-1')?.run();
    expect(openStrategicContextTarget).toHaveBeenCalledWith({ surface: 'task', task_id: 'task-1' });

    captured.find(command => command.id === 'strategic.session.metric.runtime.compute_failed')?.run();
    expect(openStrategicContextTarget).toHaveBeenCalledWith({ surface: 'runtime', section: 'compute' });
    expect(captured.filter(command => command.id.startsWith('strategic.session.metric.'))).toHaveLength(10);
    expect(captured.find(command => command.id === 'strategic.session.metric.messages.open')).toBeUndefined();
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('routes visible work session message reply actions to the message target', () => {
    const openStrategicContextTarget = vi.fn();
    const openStrategicMessageTarget = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const strategicState = {
      work_session: {
        actions: [
          {
            id: 'action-reply',
            kind: 'reply',
            label: 'Reply to operator question',
            reason: 'Strategic needs a direct answer',
            priority: 'critical',
            source: 'message',
            target: { surface: 'messages', message_id: 'msg-action', action: 'reply' },
            enabled: true,
          },
        ],
      },
    } as StrategicStateResponse;

    mount({ strategicState, openStrategicContextTarget, openStrategicMessageTarget, setCommandPaletteOpen });

    const command = captured.find(item => item.id === 'strategic.session.action.action-reply');
    expect(command?.label).toBe('Reply to operator question');
    expect(command?.detail).toBe('Strategic needs a direct answer / critical');
    command?.run();

    expect(openStrategicMessageTarget).toHaveBeenCalledWith({ messageId: 'msg-action', action: 'reply' });
    expect(openStrategicContextTarget).not.toHaveBeenCalled();
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('routes work session task actions through the task target convention', () => {
    const openStrategicContextTarget = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const target = { surface: 'task', task_id: 'task-action', artifact_id: 'artifact-action' } as const;
    const strategicState = {
      work_session: {
        actions: [
          {
            id: 'action-review-task',
            kind: 'review',
            label: 'Review rollout task',
            reason: 'The active task needs operator judgment',
            priority: 'high',
            source: 'task',
            target,
            enabled: true,
          },
        ],
      },
    } as StrategicStateResponse;

    mount({ strategicState, openStrategicContextTarget, setCommandPaletteOpen });

    captured.find(command => command.id === 'strategic.session.action.action-review-task')?.run();

    expect(openStrategicContextTarget).toHaveBeenCalledWith(target);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('preserves canonical runtime, pipeline, and knowledge work session targets', () => {
    const openStrategicContextTarget = vi.fn();
    const runtimeTarget = { surface: 'runtime', section: 'compute', task_id: 'task-runtime' } as const;
    const pipelineTarget = { surface: 'pipeline', section: 'board', task_id: 'task-pipeline' } as const;
    const knowledgeTarget = {
      surface: 'knowledge',
      document_id: 'kdoc-action',
      artifact_id: 'artifact-action',
      direction_id: 'direction-action',
    } as const;
    const actions: StrategicWorkSessionAction[] = [
      {
        id: 'action-runtime',
        kind: 'inspect',
        label: 'Inspect runtime work',
        reason: 'Runtime has a recent failure',
        priority: 'normal',
        source: 'runtime',
        target: runtimeTarget,
        enabled: true,
      },
      {
        id: 'action-pipeline',
        kind: 'open',
        label: 'Open pipeline board',
        reason: 'The board has queued work',
        priority: 'normal',
        source: 'session',
        target: pipelineTarget,
        enabled: true,
      },
      {
        id: 'action-knowledge',
        kind: 'open',
        label: 'Open evidence note',
        reason: 'Evidence should retain context target fields',
        priority: 'normal',
        source: 'context',
        target: knowledgeTarget,
        enabled: true,
      },
    ];
    const strategicState = {
      work_session: { actions },
    } as StrategicStateResponse;

    mount({ strategicState, openStrategicContextTarget });

    captured.find(command => command.id === 'strategic.session.action.action-runtime')?.run();
    captured.find(command => command.id === 'strategic.session.action.action-pipeline')?.run();
    captured.find(command => command.id === 'strategic.session.action.action-knowledge')?.run();

    expect(openStrategicContextTarget).toHaveBeenCalledWith(runtimeTarget);
    expect(openStrategicContextTarget).toHaveBeenCalledWith(pipelineTarget);
    expect(openStrategicContextTarget).toHaveBeenCalledWith(knowledgeTarget);
  });

  test('disables blocked visible work session actions and hides actions outside the visible cap', () => {
    const openStrategicContextTarget = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const strategicState = {
      attention: {
        items: [
          {
            id: 'message:msg-1',
            kind: 'message',
            severity: 'needs_operator',
            reason: 'question',
            label: 'Question waiting',
            detail: 'This should be covered by the work session action',
            ref: { kind: 'message', id: 'msg-1', project_id: 'project-1' },
          },
        ],
      },
      work_session: {
        actions: [
          {
            id: 'action-blocked',
            kind: 'inspect',
            label: 'Inspect held runtime',
            reason: 'Runtime needs a check',
            priority: 'high',
            source: 'runtime',
            target: { surface: 'runtime', section: 'compute' },
            enabled: false,
            blocked_reason: 'Waiting for approval window',
          },
          {
            id: 'attention.message:msg-1',
            kind: 'reply',
            label: 'Reply from next actions',
            reason: 'A question is waiting',
            priority: 'critical',
            source: 'attention',
            target: { surface: 'messages', message_id: 'msg-1' },
            enabled: true,
          },
          {
            id: 'action-third',
            kind: 'open',
            label: 'Open runtime health',
            reason: 'Runtime health is relevant',
            priority: 'normal',
            source: 'runtime',
            target: { surface: 'runtime', section: 'health' },
            enabled: true,
          },
          {
            id: 'action-fourth',
            kind: 'open',
            label: 'Open pipeline flow',
            reason: 'Pipeline flow is relevant',
            priority: 'normal',
            source: 'session',
            target: { surface: 'pipeline', section: 'flow' },
            enabled: true,
          },
          {
            id: 'action-hidden',
            kind: 'open',
            label: 'Hidden fifth action',
            reason: 'Only visible next actions should become commands',
            priority: 'low',
            source: 'session',
            target: { surface: 'pipeline', section: 'archive' },
            enabled: true,
          },
        ],
      },
    } as StrategicStateResponse;

    mount({ strategicState, openStrategicContextTarget, setCommandPaletteOpen });

    const blockedCommand = captured.find(command => command.id === 'strategic.session.action.action-blocked');
    expect(blockedCommand?.disabled).toBe(true);
    expect(blockedCommand?.detail).toBe('Waiting for approval window / high');

    expect(openStrategicContextTarget).not.toHaveBeenCalled();
    expect(setCommandPaletteOpen).not.toHaveBeenCalled();
    expect(captured.find(command => command.id === 'strategic.session.action.action-hidden')).toBeUndefined();
    expect(captured.find(command => command.id === 'strategic.session.action.attention.message:msg-1')).toBeDefined();
    expect(captured.find(command => command.id === 'strategic.attention.message.message:msg-1')).toBeUndefined();
  });

  test('routes pipeline and runtime commands without introducing backend actions', () => {
    const handleTabChange = vi.fn();
    const setPipelineSection = vi.fn<(section: PipelineSection) => void>();
    const setRuntimeSection = vi.fn<(section: RuntimeSection) => void>();
    const setRuntimeConfigSection = vi.fn<(section: RuntimeSettingsSection) => void>();
    const setViewMode = vi.fn<(mode: ViewMode) => void>();
    const setShowArchive = vi.fn<(show: boolean) => void>();
    mount({ handleTabChange, setPipelineSection, setRuntimeSection, setRuntimeConfigSection, setViewMode, setShowArchive });

    captured.find(command => command.id === 'pipeline.board')?.run();
    captured.find(command => command.id === 'runtime.analytics')?.run();
    captured.find(command => command.id === 'runtime.settings')?.run();

    expect(setPipelineSection).toHaveBeenCalledWith('board');
    expect(setViewMode).toHaveBeenCalledWith('kanban');
    expect(setShowArchive).toHaveBeenCalledWith(false);
    expect(setRuntimeSection).toHaveBeenCalledWith('analytics');
    expect(setRuntimeSection).toHaveBeenCalledWith('settings');
    expect(setRuntimeConfigSection).toHaveBeenCalledWith('orchestrator');
    expect(handleTabChange).toHaveBeenCalledWith('pipeline');
    expect(handleTabChange).toHaveBeenCalledWith('runtime');
  });

  test('disables project-scoped commands when no project is selected', () => {
    mount({ selectedProjectId: null, selectedProjectRepoLabel: null });

    expect(captured.find(command => command.id === 'strategic.focus')?.disabled).toBe(true);
    expect(captured.filter(command => command.id.startsWith('strategic.mode.')).every(command => command.disabled)).toBe(true);
    expect(captured.find(command => command.id === 'strategic.context.workspace')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.members')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'pipeline.flow')?.disabled).toBeFalsy();
    expect(captured.find(command => command.id === 'runtime.health')?.disabled).toBeFalsy();
    expect(captured.find(command => command.id === 'runtime.analytics')?.disabled).toBeFalsy();
  });

  test('disables Strategic mode commands while saving without blocking navigation commands', () => {
    mount({ savingStrategicWorkbench: true });

    expect(captured.filter(command => command.id.startsWith('strategic.mode.')).every(command => command.disabled)).toBe(true);
    expect(captured.find(command => command.id === 'strategic.focus')?.disabled).toBeFalsy();
    expect(captured.find(command => command.id === 'pipeline.flow')?.disabled).toBeFalsy();
  });

  test('opens the supplied activity surface instead of a stale global flag', () => {
    const setShowActivityFeed = vi.fn<(show: boolean) => void>();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    mount({ setShowActivityFeed, setCommandPaletteOpen });

    captured.find(command => command.id === 'activity.open')?.run();

    expect(setShowActivityFeed).toHaveBeenCalledWith(true);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('routes refresh, task launch, settings, and project management commands through existing callbacks', () => {
    const refreshWorkbench = vi.fn();
    const openNewTaskModal = vi.fn();
    const openGlobalSettings = vi.fn();
    const openProjectManagement = vi.fn();
    const openProjectMembers = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    mount({
      refreshWorkbench,
      openNewTaskModal,
      openGlobalSettings,
      openProjectManagement,
      openProjectMembers,
      setCommandPaletteOpen,
    });

    captured.find(command => command.id === 'workbench.refresh')?.run();
    captured.find(command => command.id === 'task.new')?.run();
    captured.find(command => command.id === 'settings.general')?.run();
    captured.find(command => command.id === 'project.switcher')?.run();
    captured.find(command => command.id === 'project.members')?.run();

    expect(refreshWorkbench).toHaveBeenCalledTimes(1);
    expect(openNewTaskModal).toHaveBeenCalledTimes(1);
    expect(openGlobalSettings).toHaveBeenCalledWith('general');
    expect(openProjectManagement).toHaveBeenCalledTimes(1);
    expect(openProjectMembers).toHaveBeenCalledTimes(1);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('exposes common keyboard shortcut hints for discoverability', () => {
    mount();

    expect(captured.find(command => command.id === 'strategic.focus')?.shortcut).toBe('1');
    expect(captured.find(command => command.id === 'pipeline.flow')?.shortcut).toBe('2');
    expect(captured.find(command => command.id === 'project.update')?.shortcut).toBe('3');
    expect(captured.find(command => command.id === 'runtime.health')?.shortcut).toBe('4');
    expect(captured.find(command => command.id === 'task.new')?.shortcut).toBe('N');
    expect(captured.find(command => command.id === 'workbench.refresh')?.shortcut).toBe('R');
    expect(captured.find(command => command.id === 'settings.general')?.shortcut).toBe('S');
  });

  test('builds palette without throwing on non-numeric metric value', () => {
    const openStrategicContextTarget = vi.fn();
    const strategicState = {
      work_session: {
        curated_metrics: [
          {
            id: 'metric.string_value',
            label: 'string metric',
            value: 'high' as unknown as number,
            tone: 'critical',
            target: { surface: 'project', section: 'running-update' },
          },
          {
            id: 'metric.null_value',
            label: 'null metric',
            value: null as unknown as number,
            tone: 'warning',
            target: { surface: 'project', section: 'running-update' },
          },
        ],
      },
    } as StrategicStateResponse;

    expect(() => mount({ strategicState, openStrategicContextTarget })).not.toThrow();
    const stringCmd = captured.find(command => command.id === 'strategic.session.metric.metric.string_value');
    expect(stringCmd).toBeDefined();
    expect(stringCmd?.detail).toContain('high');
  });

  test('disables design command when no design doc exists', () => {
    mount({ hasDesignDoc: false });

    expect(captured.find(command => command.id === 'project.design')?.disabled).toBe(true);
  });

  test('enables design command when design doc exists', () => {
    mount({ hasDesignDoc: true });

    expect(captured.find(command => command.id === 'project.design')?.disabled).toBe(false);
  });
});
