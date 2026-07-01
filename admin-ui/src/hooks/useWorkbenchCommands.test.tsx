import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useEffect } from 'react';
import { useWorkbenchCommands, type PipelineSection } from './useWorkbenchCommands';
import type { ProjectManageSection, ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import type { StrategicInteractionMode } from '../components/StrategicSessionControl';
import type { StrategicStateResponse, StrategicWorkbenchState, StrategicWorkSessionAction, StrategicWorkSessionCommand } from '../strategicStateApi';
import type { ViewMode } from './useViewPreferences';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: apiFetchMock,
}));

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
    isAdmin: true,
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
    setProjectManageSection: vi.fn(),
    setRuntimeSection: vi.fn(),
    setRuntimeConfigSection: vi.fn(),
    setViewMode: vi.fn(),
    setShowArchive: vi.fn(),
    setShowActivityFeed: vi.fn(),
    setShowResearchBrowser: vi.fn(),
    setShowKnowledgeBrowser: vi.fn(),
    openNewTaskModal: vi.fn(),
    openGlobalSettings: vi.fn(),
    closeGlobalSettings: vi.fn(),
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

  beforeEach(() => {
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue(new Response('{}', { status: 200 }));
  });

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

  test('exposes the durable Strategic check-in action with server-backed session detail', () => {
    const markStrategicChecked = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const checkedAt = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString();
    const strategicState = {
      work_session: {
        last_check_in: {
          message_id: 'msg-check',
          created_at: checkedAt,
          kind: 'manual',
        },
        since_last_check_in: {
          version: 1,
          available: true,
          since: checkedAt,
          until: new Date().toISOString(),
          messages: { total: 2 },
          tasks: { created: 1, updated: 4 },
          alerts: { raised: 1 },
          strategic: { cycles_completed: 0 },
        },
      },
    } as StrategicStateResponse;

    mount({ strategicState, markStrategicChecked, setCommandPaletteOpen });

    const command = captured.find(item => item.id === 'strategic.check_in.mark');
    expect(command).toMatchObject({
      label: 'Mark Strategic checked',
      section: 'Strategic',
      disabled: false,
    });
    expect(command?.detail).toContain('Last');
    expect(command?.detail).toContain('7 new');
    expect(command?.keywords).toContain('daily');

    command?.run();

    expect(markStrategicChecked).toHaveBeenCalledTimes(1);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('disables the Strategic check-in command while a receipt is being recorded', () => {
    mount({
      checkingInStrategicSession: true,
      markStrategicChecked: vi.fn(),
    });

    expect(captured.find(command => command.id === 'strategic.check_in.mark')).toMatchObject({
      detail: 'Recording check-in',
      disabled: true,
    });
  });

  test('routes backend work session command descriptors through existing callbacks', () => {
    const markStrategicChecked = vi.fn();
    const switchStrategicInteractionMode = vi.fn<(mode: StrategicInteractionMode) => void>();
    const openStrategicMessageTarget = vi.fn();
    const openStrategicContextTarget = vi.fn();
    const updateStrategicSessionRefs = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const runtimeTarget = { surface: 'runtime', section: 'compute', task_id: 'task-runtime' } as const;
    const commands: StrategicWorkSessionCommand[] = [
      {
        id: 'session.check_in',
        label: 'Check in',
        group: 'session',
        priority: 'normal',
        enabled: true,
        target: { surface: 'messages', action: 'check_in' },
      },
      {
        id: 'session.mode.continuous',
        label: 'Workday mode',
        group: 'mode',
        priority: 'normal',
        enabled: true,
        target: { surface: 'messages', action: 'set_continuous_mode' },
      },
      {
        id: 'session.clear_selected_refs',
        label: 'Clear selected refs',
        group: 'context',
        priority: 'normal',
        enabled: true,
        target: { surface: 'project', section: 'selected_refs', action: 'clear' },
      },
      {
        id: 'attention.message:msg-command',
        label: 'Reply to Strategic question',
        group: 'attention',
        priority: 'critical',
        enabled: true,
        target: { surface: 'messages', message_id: 'msg-command', action: 'reply' },
      },
      {
        id: 'runtime.inspect',
        label: 'Inspect runtime hold',
        group: 'runtime',
        priority: 'high',
        enabled: true,
        target: runtimeTarget,
      },
    ];
    const strategicState = {
      work_session: {
        selected_refs: [
          { kind: 'task', id: 'task-1', label: 'Task 1' },
          { kind: 'artifact', id: 'artifact-1', label: 'Report' },
        ],
        commands,
        actions: [
          {
            id: 'attention.message:msg-command',
            kind: 'reply',
            label: 'Duplicate local action',
            reason: 'Descriptor should own this command',
            priority: 'critical',
            source: 'attention',
            target: { surface: 'messages', message_id: 'msg-command', action: 'reply' },
            enabled: true,
          },
        ],
      },
    } as StrategicStateResponse;

    mount({
      strategicState,
      markStrategicChecked,
      switchStrategicInteractionMode,
      openStrategicMessageTarget,
      openStrategicContextTarget,
      updateStrategicSessionRefs,
      setCommandPaletteOpen,
    });

    expect(captured.find(command => command.id === 'strategic.check_in.mark')).toBeUndefined();
    expect(captured.find(command => command.id === 'strategic.mode.continuous')).toBeUndefined();
    expect(captured.find(command => command.id === 'strategic.session.action.attention.message:msg-command')).toBeUndefined();
    expect(captured.find(command => command.id === 'session.check_in')).toMatchObject({
      label: 'Check in',
      section: 'Strategic',
      detail: 'Never checked',
      disabled: false,
    });
    expect(captured.find(command => command.id === 'session.clear_selected_refs')?.detail).toBe('2 selected');
    expect(captured.find(command => command.id === 'runtime.inspect')).toMatchObject({
      section: 'Runtime',
      detail: 'Runtime compute / high',
    });

    captured.find(command => command.id === 'session.check_in')?.run();
    captured.find(command => command.id === 'session.mode.continuous')?.run();
    captured.find(command => command.id === 'session.clear_selected_refs')?.run();
    captured.find(command => command.id === 'attention.message:msg-command')?.run();
    captured.find(command => command.id === 'runtime.inspect')?.run();

    expect(markStrategicChecked).toHaveBeenCalledTimes(1);
    expect(switchStrategicInteractionMode).toHaveBeenCalledWith('continuous');
    expect(openStrategicMessageTarget).toHaveBeenCalledWith({ messageId: 'msg-command', action: 'reply' });
    expect(openStrategicContextTarget).toHaveBeenCalledWith(runtimeTarget);
    expect(updateStrategicSessionRefs).toHaveBeenCalledWith(expect.any(Function));
    const updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
    expect(updater([{ kind: 'task', id: 'task-1', label: 'Task 1' }])).toEqual([]);
    expect(apiFetchMock).not.toHaveBeenCalled();
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('executes backend mutation descriptors for Strategic mode changes and refreshes workbench', async () => {
    const switchStrategicInteractionMode = vi.fn<(mode: StrategicInteractionMode) => void>();
    const refreshWorkbench = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const mutationBody = { workbench: { interaction_mode: 'monitor' } };
    const commands: StrategicWorkSessionCommand[] = [
      {
        id: 'session.mode.monitor',
        label: 'Monitor mode',
        group: 'mode',
        priority: 'normal',
        enabled: true,
        target: { surface: 'messages', action: 'set_monitor_mode' },
        mutation: {
          method: 'PATCH',
          path: '/api/admin/projects/project-1/strategic-session',
          body: mutationBody,
        },
      },
    ];

    mount({
      strategicState: { work_session: { commands } } as StrategicStateResponse,
      switchStrategicInteractionMode,
      refreshWorkbench,
      setCommandPaletteOpen,
    });

    const command = captured.find(item => item.id === 'session.mode.monitor');
    expect(command).toMatchObject({ detail: 'Set session mode', disabled: false });

    command?.run();

    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
    expect(switchStrategicInteractionMode).not.toHaveBeenCalled();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/projects/project-1/strategic-session', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(mutationBody),
    });
    await vi.waitFor(() => {
      expect(refreshWorkbench).toHaveBeenCalledTimes(1);
    });
  });

  test('uses backend mutation descriptors to clear selected refs when present', async () => {
    const updateStrategicSessionRefs = vi.fn();
    const refreshWorkbench = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const mutationBody = { workbench: { selected_refs: [] } };
    const commands: StrategicWorkSessionCommand[] = [
      {
        id: 'session.clear_selected_refs',
        label: 'Clear selected refs',
        group: 'context',
        priority: 'normal',
        enabled: true,
        target: { surface: 'project', section: 'selected_refs', action: 'clear' },
        mutation: {
          method: 'POST',
          path: '/api/admin/projects/project-1/strategic-session',
          body: mutationBody,
        },
      },
    ];
    const strategicState = {
      work_session: {
        selected_refs: [
          { kind: 'task', id: 'task-1', label: 'Task 1' },
          { kind: 'artifact', id: 'artifact-1', label: 'Report' },
        ],
        commands,
      },
    } as StrategicStateResponse;

    mount({
      strategicState,
      updateStrategicSessionRefs,
      refreshWorkbench,
      setCommandPaletteOpen,
    });

    const command = captured.find(item => item.id === 'session.clear_selected_refs');
    expect(command).toMatchObject({ detail: '2 selected', disabled: false });

    command?.run();

    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
    expect(updateStrategicSessionRefs).not.toHaveBeenCalled();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/projects/project-1/strategic-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(mutationBody),
    });
    await vi.waitFor(() => {
      expect(refreshWorkbench).toHaveBeenCalledTimes(1);
    });
  });

  test('executes backend mutation descriptors for runner preference changes', async () => {
    const refreshWorkbench = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const preferBody = { workbench: { preferred_runner_id: 'runner-1' } };
    const clearBody = { workbench: { preferred_runner_id: null } };
    const commands: StrategicWorkSessionCommand[] = [
      {
        id: 'session.runner.prefer.runner-1',
        label: 'Prefer desk-mini',
        group: 'runtime',
        priority: 'normal',
        enabled: true,
        target: {
          surface: 'runtime',
          section: 'devices',
          action: 'prefer_runner',
          runner_id: 'runner-1',
        },
        mutation: {
          method: 'POST',
          path: '/api/admin/projects/project-1/strategic-session',
          body: preferBody,
        },
      },
      {
        id: 'session.runner.clear_preferred',
        label: 'Clear preferred runner',
        group: 'runtime',
        priority: 'normal',
        enabled: true,
        target: {
          surface: 'runtime',
          section: 'devices',
          action: 'clear_preferred_runner',
          runner_id: 'runner-1',
        },
        mutation: {
          method: 'POST',
          path: '/api/admin/projects/project-1/strategic-session',
          body: clearBody,
        },
      },
    ];

    mount({
      strategicState: { work_session: { commands } } as StrategicStateResponse,
      refreshWorkbench,
      setCommandPaletteOpen,
    });

    expect(captured.find(item => item.id === 'session.runner.prefer.runner-1')).toMatchObject({
      section: 'Runtime',
      detail: 'Runtime devices',
      disabled: false,
    });
    expect(captured.find(item => item.id === 'session.runner.clear_preferred')).toMatchObject({
      section: 'Runtime',
      detail: 'Runtime devices',
      disabled: false,
    });

    captured.find(item => item.id === 'session.runner.prefer.runner-1')?.run();
    captured.find(item => item.id === 'session.runner.clear_preferred')?.run();

    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
    expect(apiFetchMock).toHaveBeenNthCalledWith(1, '/api/admin/projects/project-1/strategic-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(preferBody),
    });
    expect(apiFetchMock).toHaveBeenNthCalledWith(2, '/api/admin/projects/project-1/strategic-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(clearBody),
    });
    await vi.waitFor(() => {
      expect(refreshWorkbench).toHaveBeenCalledTimes(2);
    });
  });

  test.each([
    ['subpath', { method: 'PATCH', path: '/api/admin/projects/project-1/strategic-session/messages' }],
    ['wrong project', { method: 'PATCH', path: '/api/admin/projects/other-project/strategic-session' }],
    ['disallowed method', { method: 'DELETE', path: '/api/admin/projects/project-1/strategic-session' }],
  ])('falls back to local command handling for unsafe backend mutation descriptors: %s', (_caseName, mutation) => {
    const switchStrategicInteractionMode = vi.fn<(mode: StrategicInteractionMode) => void>();
    const refreshWorkbench = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const commands: StrategicWorkSessionCommand[] = [
      {
        id: 'session.mode.monitor',
        label: 'Monitor mode',
        group: 'mode',
        priority: 'normal',
        enabled: true,
        target: { surface: 'messages', action: 'set_monitor_mode' },
        mutation: {
          ...mutation,
          body: { workbench: { interaction_mode: 'monitor' } },
        },
      },
    ];

    mount({
      strategicState: { work_session: { commands } } as StrategicStateResponse,
      switchStrategicInteractionMode,
      refreshWorkbench,
      setCommandPaletteOpen,
    });

    captured.find(item => item.id === 'session.mode.monitor')?.run();

    expect(apiFetchMock).not.toHaveBeenCalled();
    expect(refreshWorkbench).not.toHaveBeenCalled();
    expect(switchStrategicInteractionMode).toHaveBeenCalledWith('monitor');
  });

  test('does not execute unsafe runner preference mutation descriptors', () => {
    const refreshWorkbench = vi.fn();
    const openStrategicContextTarget = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const target = {
      surface: 'runtime',
      section: 'devices',
      action: 'prefer_runner',
      runner_id: 'runner-1',
    } as const;
    const commands: StrategicWorkSessionCommand[] = [
      {
        id: 'session.runner.prefer.runner-1',
        label: 'Prefer desk-mini',
        group: 'runtime',
        priority: 'normal',
        enabled: true,
        target,
        mutation: {
          method: 'POST',
          path: '/api/admin/projects/other-project/strategic-session',
          body: { workbench: { preferred_runner_id: 'runner-1' } },
        },
      },
    ];

    mount({
      strategicState: { work_session: { commands } } as StrategicStateResponse,
      refreshWorkbench,
      openStrategicContextTarget,
      setCommandPaletteOpen,
    });

    captured.find(item => item.id === 'session.runner.prefer.runner-1')?.run();

    expect(apiFetchMock).not.toHaveBeenCalled();
    expect(refreshWorkbench).not.toHaveBeenCalled();
    expect(openStrategicContextTarget).toHaveBeenCalledWith(target);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('routes message descriptors through context navigation when message opener is unavailable', () => {
    const openStrategicContextTarget = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const target = { surface: 'messages', message_id: 'msg-command', action: 'reply' } as const;
    const commands: StrategicWorkSessionCommand[] = [
      {
        id: 'attention.message:msg-command',
        label: 'Reply to Strategic question',
        group: 'attention',
        priority: 'critical',
        enabled: true,
        target,
      },
    ];
    const strategicState = {
      work_session: {
        commands,
        actions: [
          {
            id: 'attention.message:msg-command',
            kind: 'reply',
            label: 'Duplicate local action',
            reason: 'Descriptor should preserve the route',
            priority: 'critical',
            source: 'attention',
            target,
            enabled: true,
          },
        ],
      },
    } as StrategicStateResponse;

    mount({ strategicState, openStrategicContextTarget, setCommandPaletteOpen });

    const command = captured.find(item => item.id === 'attention.message:msg-command');
    expect(command).toMatchObject({ disabled: false, section: 'Messages' });
    expect(captured.find(item => item.id === 'strategic.session.action.attention.message:msg-command')).toBeUndefined();

    command?.run();

    expect(openStrategicContextTarget).toHaveBeenCalledWith(target);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('honors disabled descriptor state and current Strategic mode', () => {
    const markStrategicChecked = vi.fn();
    const switchStrategicInteractionMode = vi.fn<(mode: StrategicInteractionMode) => void>();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const commands: StrategicWorkSessionCommand[] = [
      {
        id: 'session.mode.chat',
        label: 'Chat mode',
        group: 'mode',
        priority: 'normal',
        enabled: true,
        target: { surface: 'messages', action: 'set_chat_mode' },
      },
      {
        id: 'session.mode.monitor',
        label: 'Monitor mode',
        group: 'mode',
        priority: 'normal',
        enabled: false,
        target: { surface: 'messages', action: 'set_monitor_mode' },
      },
      {
        id: 'session.check_in',
        label: 'Check in',
        group: 'session',
        priority: 'normal',
        enabled: true,
        target: { surface: 'messages', action: 'check_in' },
      },
    ];

    mount({
      strategicInteractionMode: 'chat',
      checkingInStrategicSession: true,
      strategicState: { work_session: { commands } } as StrategicStateResponse,
      markStrategicChecked,
      switchStrategicInteractionMode,
      setCommandPaletteOpen,
    });

    const currentMode = captured.find(command => command.id === 'session.mode.chat');
    expect(currentMode).toMatchObject({ detail: 'Current mode', disabled: true });
    currentMode?.run();

    const disabledMode = captured.find(command => command.id === 'session.mode.monitor');
    expect(disabledMode).toMatchObject({ detail: 'Unavailable', disabled: true });
    disabledMode?.run();

    const checkIn = captured.find(command => command.id === 'session.check_in');
    expect(checkIn).toMatchObject({ detail: 'Recording check-in', disabled: true });
    checkIn?.run();

    expect(switchStrategicInteractionMode).not.toHaveBeenCalled();
    expect(markStrategicChecked).not.toHaveBeenCalled();
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('keeps legacy session and action fallbacks when descriptors are absent', () => {
    const markStrategicChecked = vi.fn();
    const switchStrategicInteractionMode = vi.fn<(mode: StrategicInteractionMode) => void>();
    const openStrategicMessageTarget = vi.fn();
    const action: StrategicWorkSessionAction = {
      id: 'action-reply-fallback',
      kind: 'reply',
      label: 'Reply from fallback action',
      reason: 'No backend command descriptor was provided',
      priority: 'critical',
      source: 'attention',
      target: { surface: 'messages', message_id: 'msg-fallback', action: 'reply' },
      enabled: true,
    };

    mount({
      strategicState: { work_session: { actions: [action] } } as StrategicStateResponse,
      markStrategicChecked,
      switchStrategicInteractionMode,
      openStrategicMessageTarget,
    });

    expect(captured.find(command => command.id === 'session.check_in')).toBeUndefined();
    expect(captured.find(command => command.id === 'strategic.check_in.mark')).toBeDefined();
    expect(captured.find(command => command.id === 'strategic.mode.continuous')).toBeDefined();
    expect(captured.find(command => command.id === 'strategic.session.action.action-reply-fallback')).toBeDefined();

    captured.find(command => command.id === 'strategic.check_in.mark')?.run();
    captured.find(command => command.id === 'strategic.mode.continuous')?.run();
    captured.find(command => command.id === 'strategic.session.action.action-reply-fallback')?.run();

    expect(markStrategicChecked).toHaveBeenCalledTimes(1);
    expect(switchStrategicInteractionMode).toHaveBeenCalledWith('continuous');
    expect(openStrategicMessageTarget).toHaveBeenCalledWith({ messageId: 'msg-fallback', action: 'reply' });
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
        { kind: 'canvas', id: 'strategic-canvas:project-1', label: 'Strategic context map' },
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

    const contextCommand = captured.find(command => command.id === 'strategic.context.workspace');
    expect(contextCommand?.label).toBe('Strategic context');
    expect(contextCommand?.keywords).toContain('canvas');
    expect(contextCommand?.detail).toBe('3 kept, 2 resources, 1 nodes');
    contextCommand?.run();
    expect(openStrategicContextWorkspace).toHaveBeenCalledTimes(1);

    expect(captured.find(command => command.id === 'strategic.context.map')?.detail).toBe('3 kept, 2 resources, 1 nodes');
    expect(captured.find(command => command.id === 'strategic.context.open.resource.canvas:strategic-canvas:project-1')).toBeUndefined();
    expect(captured.find(command => command.id === 'strategic.context.keep.canvas:strategic-canvas:project-1')).toBeUndefined();
    expect(captured.find(command => command.id === 'strategic.context.remove.canvas:strategic-canvas:project-1')).toMatchObject({
      label: 'Remove from Strategic context: Strategic context map',
      detail: 'context map / kept',
    });
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

    captured.find(command => command.id === 'strategic.context.remove.canvas:strategic-canvas:project-1')?.run();
    expect(updateStrategicSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    let updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
    expect(updater([
      { kind: 'canvas', id: 'strategic-canvas:project-1', label: 'Strategic context map' },
      { kind: 'artifact', id: 'artifact:latest', label: 'Latest report' },
    ])).toEqual([
      { kind: 'artifact', id: 'artifact:latest', label: 'Latest report' },
    ]);

    captured.find(command => command.id === 'strategic.context.keep.knowledge_document:knowledge:operator-notes')?.run();
    expect(updateStrategicSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
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

  test('offers a whole-map keep command when the context package has a canvas but no canvas resource', () => {
    const updateStrategicSessionRefs = vi.fn();
    const strategicState = {
      context_package: {
        canvas: {
          id: 'canvas:synthetic',
          title: 'Strategic discussion map',
          node_count: 4,
          edge_count: 5,
          nodes: [],
          edges: [],
        },
        resources: [
          {
            kind: 'artifact',
            id: 'artifact:report',
            label: 'Report',
            summary: 'A regular resource',
          },
        ],
      },
    } as StrategicStateResponse;

    mount({ strategicState, updateStrategicSessionRefs });

    const keepMap = captured.find(command => command.id === 'strategic.context.keep.canvas:canvas:synthetic');
    expect(keepMap).toMatchObject({
      label: 'Keep in Strategic context: Strategic discussion map',
      detail: 'context map / 4 nodes / 5 links',
    });
    expect(captured.find(command => command.id === 'strategic.context.keep.artifact:artifact:report')).toBeDefined();

    keepMap?.run();

    expect(updateStrategicSessionRefs).toHaveBeenCalledWith(expect.any(Function));
    const updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
    expect(updater([])).toEqual([
      { kind: 'canvas', id: 'canvas:synthetic', label: 'Strategic discussion map' },
    ]);
  });

  test('uses the canvas resource summary when keeping an unselected explicit map resource', () => {
    const updateStrategicSessionRefs = vi.fn();
    const strategicState = {
      context_package: {
        resources: [
          {
            kind: 'canvas',
            id: 'canvas:explicit',
            label: 'Explicit map resource',
            summary: '7 nodes / 8 links',
          },
        ],
        canvas: {
          id: 'canvas:explicit',
          title: 'Explicit map resource',
          node_count: 7,
          edge_count: 8,
          nodes: [],
          edges: [],
        },
      },
    } as StrategicStateResponse;

    mount({ strategicState, updateStrategicSessionRefs });

    const keepMap = captured.find(command => command.id === 'strategic.context.keep.canvas:canvas:explicit');
    expect(keepMap).toMatchObject({
      label: 'Keep in Strategic context: Explicit map resource',
      detail: 'context map / 7 nodes / 8 links',
    });
    expect(captured.find(command => command.id === 'strategic.context.open.resource.canvas:canvas:explicit')).toBeUndefined();
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

  test('exposes capped first-class message and task-focus commands', () => {
    const openStrategicMessageTarget = vi.fn();
    const openStrategicContextTarget = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const strategicState = {
      messages: {
        counts: { open: 2, linked: 1 },
        items: [
          {
            id: 'msg-question',
            project_id: 'project-1',
            message_type: 'QUESTION',
            from_type: 'strategic',
            from_task_id: null,
            content: 'Pick a runner for local Strategic?',
            context: {},
            status: 'open',
            resolution: null,
            resolved_at: null,
            created_at: '2026-06-30T12:00:00Z',
          },
          {
            id: 'msg-blocker',
            project_id: 'project-1',
            message_type: 'BLOCKER',
            from_type: 'system',
            from_task_id: null,
            content: 'Runner heartbeat stalled.',
            context: {},
            status: 'linked',
            resolution: null,
            resolved_at: null,
            created_at: '2026-06-30T12:01:00Z',
          },
        ],
      },
      tasks: {
        focus: {
          version: 1,
          bucket_limit: 6,
          has_more: false,
          buckets: {
            needs_judgment: {
              total: 1,
              limit: 6,
              has_more: false,
              items: [
                {
                  id: 'task-judgment',
                  bucket: 'needs_judgment',
                  status: 'EVALUATING',
                  title: 'Review runner registration',
                  task_type: 'review',
                  detail: 'Operator judgment needed',
                  requires_approval: true,
                  reasons: ['approval'],
                },
              ],
            },
            active: {
              total: 1,
              limit: 6,
              has_more: false,
              items: [
                {
                  id: 'task-active',
                  bucket: 'active',
                  status: 'IN_PROGRESS',
                  title: 'Polish command palette',
                  task_type: 'fix',
                  latest_status_message: 'Adding task-focus command routes',
                  requires_approval: false,
                  reasons: [],
                },
              ],
            },
          },
        },
      },
    } as StrategicStateResponse;

    mount({ strategicState, openStrategicMessageTarget, openStrategicContextTarget, setCommandPaletteOpen });

    expect(captured.find(command => command.id === 'strategic.messages.open')).toMatchObject({
      label: 'Open Strategic messages',
      detail: '2 open, 1 linked',
    });

    captured.find(command => command.id === 'strategic.messages.reply.msg-question')?.run();
    expect(openStrategicMessageTarget).toHaveBeenCalledWith({ messageId: 'msg-question', action: 'reply' });

    captured.find(command => command.id === 'strategic.messages.highlight.msg-blocker')?.run();
    expect(openStrategicMessageTarget).toHaveBeenCalledWith({ messageId: 'msg-blocker', action: 'highlight' });

    expect(captured.find(command => command.id === 'strategic.task_focus.needs_judgment.task-judgment')).toMatchObject({
      label: 'Open task: Review runner registration',
      detail: 'Needs judgment / EVALUATING / Operator judgment needed',
    });
    captured.find(command => command.id === 'strategic.task_focus.active.task-active')?.run();
    expect(openStrategicContextTarget).toHaveBeenCalledWith({ surface: 'task', task_id: 'task-active' });
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('routes project manage subsection commands through the embedded project workspace', () => {
    const handleTabChange = vi.fn();
    const setProjectSection = vi.fn<(section: ProjectSection) => void>();
    const setProjectManageSection = vi.fn<(section: ProjectManageSection) => void>();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const openGlobalSettings = vi.fn();
    const closeGlobalSettings = vi.fn();
    mount({ handleTabChange, setProjectSection, setProjectManageSection, setCommandPaletteOpen, openGlobalSettings, closeGlobalSettings });

    captured.find(command => command.id === 'project.build')?.run();
    captured.find(command => command.id === 'project.branches')?.run();
    captured.find(command => command.id === 'project.review')?.run();
    captured.find(command => command.id === 'project.resources')?.run();
    captured.find(command => command.id === 'project.remediation')?.run();
    captured.find(command => command.id === 'project.issue_implementation')?.run();
    captured.find(command => command.id === 'project.alert_domains')?.run();
    captured.find(command => command.id === 'project.access')?.run();

    expect(setProjectSection).toHaveBeenCalledTimes(8);
    expect(setProjectSection).toHaveBeenCalledWith('manage');
    expect(setProjectManageSection.mock.calls.map(([section]) => section)).toEqual([
      'build-config',
      'branches',
      'auto-review',
      'resource-envelope',
      'auto-remediation',
      'issue-implementation',
      'alert-domains',
      'access',
    ]);
    expect(handleTabChange).toHaveBeenCalledWith('project');
    expect(openGlobalSettings).not.toHaveBeenCalled();
    expect(closeGlobalSettings).toHaveBeenCalledTimes(8);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('falls back to precise settings sections when project manage subsection setter is unavailable', () => {
    const handleTabChange = vi.fn();
    const setProjectSection = vi.fn<(section: ProjectSection) => void>();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const openGlobalSettings = vi.fn();
    mount({
      handleTabChange,
      setProjectSection,
      setProjectManageSection: undefined,
      setCommandPaletteOpen,
      openGlobalSettings,
    });

    captured.find(command => command.id === 'project.build')?.run();

    expect(setProjectSection).toHaveBeenCalledWith('manage');
    expect(handleTabChange).toHaveBeenCalledWith('project');
    expect(openGlobalSettings).toHaveBeenCalledWith('build-config');
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
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    const closeGlobalSettings = vi.fn();
    mount({ handleTabChange, setPipelineSection, setRuntimeSection, setRuntimeConfigSection, setViewMode, setShowArchive, setCommandPaletteOpen, closeGlobalSettings });

    captured.find(command => command.id === 'pipeline.board')?.run();
    captured.find(command => command.id === 'runtime.analytics')?.run();
    captured.find(command => command.id === 'runtime.devices')?.run();
    captured.find(command => command.id === 'runtime.accounts')?.run();
    captured.find(command => command.id === 'runtime.settings')?.run();
    captured.find(command => command.id === 'runtime.settings.general')?.run();
    captured.find(command => command.id === 'runtime.settings.deploy')?.run();
    captured.find(command => command.id === 'runtime.settings.provider-routing')?.run();
    captured.find(command => command.id === 'runtime.settings.provider-settings')?.run();
    captured.find(command => command.id === 'runtime.settings.notifications')?.run();
    captured.find(command => command.id === 'runtime.settings.advanced')?.run();

    expect(setPipelineSection).toHaveBeenCalledWith('board');
    expect(setViewMode).toHaveBeenCalledWith('kanban');
    expect(setShowArchive).toHaveBeenCalledWith(false);
    expect(setRuntimeSection).toHaveBeenCalledWith('analytics');
    expect(setRuntimeSection).toHaveBeenCalledWith('devices');
    expect(setRuntimeSection).toHaveBeenCalledWith('accounts');
    expect(setRuntimeSection).toHaveBeenCalledWith('settings');
    expect(setRuntimeConfigSection.mock.calls.map(([section]) => section)).toEqual([
      'orchestrator',
      'general',
      'deploy',
      'provider-routing',
      'provider-settings',
      'notifications',
      'advanced',
    ]);
    expect(handleTabChange).toHaveBeenCalledWith('pipeline');
    expect(handleTabChange).toHaveBeenCalledWith('runtime');
    expect(closeGlobalSettings).toHaveBeenCalledTimes(10);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('uses specific labels for task monitoring and runtime entry points', () => {
    mount();

    expect(captured.find(command => command.id === 'pipeline.flow')).toMatchObject({
      label: 'Pipeline tasks',
      detail: '3 active, 4 queued',
      section: 'Pipeline',
      shortcut: '2',
    });
    expect(captured.find(command => command.id === 'strategic.focus')).toMatchObject({
      label: 'Focus Strategic chat',
    });
    expect(captured.find(command => command.id === 'pipeline.board')).toMatchObject({
      label: 'Pipeline board',
    });
    expect(captured.find(command => command.id === 'pipeline.archive')).toMatchObject({
      label: 'Pipeline archive',
    });
    expect(captured.find(command => command.id === 'project.update')).toMatchObject({
      label: 'Project running update',
    });
    expect(captured.find(command => command.id === 'project.switcher')).toMatchObject({
      label: 'Manage projects',
    });
    expect(captured.find(command => command.id === 'runtime.health')).toMatchObject({
      label: 'Runtime overview',
      section: 'Runtime',
      shortcut: '4',
    });
    expect(captured.find(command => command.id === 'runtime.devices')).toMatchObject({
      label: 'Runtime devices',
      detail: 'Warm VMs and runners',
    });
    expect(captured.find(command => command.id === 'runtime.accounts')).toMatchObject({
      label: 'Runtime accounts',
      detail: '5 LLM accounts',
    });
    expect(captured.find(command => command.id === 'runtime.settings')).toMatchObject({
      label: 'Runtime orchestrator settings',
      detail: 'Orchestrator defaults',
    });
    expect(captured.find(command => command.id === 'settings.general')).toMatchObject({
      label: 'Workbench settings',
      detail: 'UI defaults and controls',
    });
  });

  test('disables project-scoped commands when no project is selected', () => {
    mount({
      selectedProjectId: null,
      selectedProjectRepoLabel: null,
      markStrategicChecked: vi.fn(),
      openStrategicContextWorkspace: vi.fn(),
      openStrategicMessageTarget: vi.fn(),
    });

    expect(captured.find(command => command.id === 'strategic.focus')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'strategic.check_in.mark')?.disabled).toBe(true);
    expect(captured.filter(command => command.id.startsWith('strategic.mode.')).every(command => command.disabled)).toBe(true);
    expect(captured.find(command => command.id === 'strategic.context.workspace')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'strategic.messages.open')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.build')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.branches')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.review')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.resources')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.remediation')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.issue_implementation')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.alert_domains')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.access')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'project.members')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'pipeline.flow')?.disabled).toBeFalsy();
    expect(captured.find(command => command.id === 'runtime.health')?.disabled).toBeFalsy();
    expect(captured.find(command => command.id === 'runtime.analytics')?.disabled).toBeFalsy();
  });

  test('disables admin-only runtime settings commands for non-admin users', () => {
    mount({ isAdmin: false });

    expect(captured.find(command => command.id === 'runtime.settings.deploy')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'runtime.settings.provider-routing')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'runtime.settings.provider-settings')?.disabled).toBe(true);
    expect(captured.find(command => command.id === 'runtime.settings.notifications')?.disabled).toBeFalsy();
    expect(captured.find(command => command.id === 'runtime.settings.advanced')?.disabled).toBeFalsy();
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
