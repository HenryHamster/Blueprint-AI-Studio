import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useEffect } from 'react';
import { useWorkbenchCommands, type PipelineSection } from './useWorkbenchCommands';
import type { ProjectSection, RuntimeSection, RuntimeSettingsSection } from '../components/OperatorWorkspaces';
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
    root = createRoot(host);
    act(() => {
      root!.render(<Harness {...overrides} />);
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
    expect(captured.find(command => command.id === 'pipeline.flow')?.disabled).toBeFalsy();
    expect(captured.find(command => command.id === 'runtime.health')?.disabled).toBeFalsy();
  });

  test('opens the supplied activity surface instead of a stale global flag', () => {
    const setShowActivityFeed = vi.fn<(show: boolean) => void>();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    mount({ setShowActivityFeed, setCommandPaletteOpen });

    captured.find(command => command.id === 'activity.open')?.run();

    expect(setShowActivityFeed).toHaveBeenCalledWith(true);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('routes refresh and project management commands through existing callbacks', () => {
    const refreshWorkbench = vi.fn();
    const openProjectManagement = vi.fn();
    const openProjectMembers = vi.fn();
    const setCommandPaletteOpen = vi.fn<(open: boolean) => void>();
    mount({ refreshWorkbench, openProjectManagement, openProjectMembers, setCommandPaletteOpen });

    captured.find(command => command.id === 'workbench.refresh')?.run();
    captured.find(command => command.id === 'project.switcher')?.run();
    captured.find(command => command.id === 'project.members')?.run();

    expect(refreshWorkbench).toHaveBeenCalledTimes(1);
    expect(openProjectManagement).toHaveBeenCalledTimes(1);
    expect(openProjectMembers).toHaveBeenCalledTimes(1);
    expect(setCommandPaletteOpen).toHaveBeenCalledWith(false);
  });

  test('exposes common keyboard shortcut hints for discoverability', () => {
    mount();

    expect(captured.find(command => command.id === 'strategic.focus')?.shortcut).toBe('Global 1');
    expect(captured.find(command => command.id === 'pipeline.flow')?.shortcut).toBe('Global 2');
    expect(captured.find(command => command.id === 'project.update')?.shortcut).toBe('Global 3');
    expect(captured.find(command => command.id === 'runtime.health')?.shortcut).toBe('Global 4');
    expect(captured.find(command => command.id === 'task.new')?.shortcut).toBe('Global N');
    expect(captured.find(command => command.id === 'workbench.refresh')?.shortcut).toBe('Global R');
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
