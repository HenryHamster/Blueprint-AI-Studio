import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useEffect } from 'react';
import { useWorkbenchCommands, type PipelineSection } from './useWorkbenchCommands';
import type { ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
import type { StrategicInteractionMode } from '../components/StrategicSessionControl';
import type { StrategicStateResponse, StrategicWorkbenchState } from '../strategicStateApi';
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
              target,
            },
          ],
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
      updateStrategicSessionRefs,
      setCommandPaletteOpen,
    });

    expect(captured.filter(command => command.label.includes('Latest report'))).toHaveLength(2);
    expect(captured.filter(command => command.id === 'strategic.context.remove.artifact:artifact:latest')).toHaveLength(1);
    expect(captured.filter(command => command.id.includes('strategic.context.open') && command.id.includes('artifact:artifact:latest'))).toHaveLength(1);

    captured.find(command => command.id === 'strategic.context.open.kept.artifact:artifact:latest')?.run();
    expect(openStrategicContextTarget).toHaveBeenCalledWith(target);

    captured.find(command => command.id === 'strategic.context.keep.knowledge_document:knowledge:operator-notes')?.run();
    expect(updateStrategicSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    let updater = updateStrategicSessionRefs.mock.calls.at(-1)?.[0] as (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs'];
    expect(updater([])).toEqual([
      { kind: 'knowledge_document', id: 'knowledge:operator-notes', label: 'Operator notes' },
    ]);

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

  test('routes pipeline and runtime commands without introducing backend actions', () => {
    const handleTabChange = vi.fn();
    const setPipelineSection = vi.fn<(section: PipelineSection) => void>();
    const setRuntimeSection = vi.fn<(section: RuntimeSection) => void>();
    const setRuntimeConfigSection = vi.fn<(section: RuntimeSettingsSection) => void>();
    const setViewMode = vi.fn<(mode: ViewMode) => void>();
    const setShowArchive = vi.fn<(show: boolean) => void>();
    mount({ handleTabChange, setPipelineSection, setRuntimeSection, setRuntimeConfigSection, setViewMode, setShowArchive });

    captured.find(command => command.id === 'pipeline.board')?.run();
    captured.find(command => command.id === 'runtime.settings')?.run();

    expect(setPipelineSection).toHaveBeenCalledWith('board');
    expect(setViewMode).toHaveBeenCalledWith('kanban');
    expect(setShowArchive).toHaveBeenCalledWith(false);
    expect(setRuntimeSection).toHaveBeenCalledWith('settings');
    expect(setRuntimeConfigSection).toHaveBeenCalledWith('orchestrator');
    expect(handleTabChange).toHaveBeenCalledWith('pipeline');
    expect(handleTabChange).toHaveBeenCalledWith('runtime');
  });

  test('disables project-scoped commands when no project is selected', () => {
    mount({ selectedProjectId: null, selectedProjectRepoLabel: null });

    expect(captured.find(command => command.id === 'strategic.focus')?.disabled).toBe(true);
    expect(captured.filter(command => command.id.startsWith('strategic.mode.')).every(command => command.disabled)).toBe(true);
    expect(captured.find(command => command.id === 'pipeline.flow')?.disabled).toBeFalsy();
    expect(captured.find(command => command.id === 'runtime.health')?.disabled).toBeFalsy();
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
    mount({ refreshWorkbench, openNewTaskModal, openGlobalSettings, openProjectManagement, openProjectMembers, setCommandPaletteOpen });

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

  test('disables design command when no design doc exists', () => {
    mount({ hasDesignDoc: false });

    expect(captured.find(command => command.id === 'project.design')?.disabled).toBe(true);
  });

  test('enables design command when design doc exists', () => {
    mount({ hasDesignDoc: true });

    expect(captured.find(command => command.id === 'project.design')?.disabled).toBe(false);
  });
});
