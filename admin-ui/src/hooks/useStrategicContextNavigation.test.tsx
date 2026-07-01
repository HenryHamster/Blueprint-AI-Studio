import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useEffect } from 'react';
import { useStrategicContextNavigation } from './useStrategicContextNavigation';
import type { StrategicContextPackageDrilldown } from '../strategicStateApi';

type OpenTarget = ReturnType<typeof useStrategicContextNavigation>;
type Target = StrategicContextPackageDrilldown['target'];

let openTarget: OpenTarget | null = null;

function Harness(overrides: Partial<Parameters<typeof useStrategicContextNavigation>[0]> = {}) {
  const callback = useStrategicContextNavigation({
    handleTabChange: vi.fn(),
    openMessages: vi.fn(),
    selectPipelineTaskById: vi.fn(),
    setKnowledgeBrowserDirectionId: vi.fn(),
    setKnowledgeBrowserInitialDocId: vi.fn(),
    setPipelineSection: vi.fn(),
    setProjectSection: vi.fn(),
    setProjectManageSection: vi.fn(),
    setRuntimeSection: vi.fn(),
    setRuntimeConfigSection: vi.fn(),
    setShowArchive: vi.fn(),
    setShowKnowledgeBrowser: vi.fn(),
    setViewMode: vi.fn(),
    ...overrides,
  });
  useEffect(() => {
    openTarget = callback;
  }, [callback]);
  return null;
}

describe('useStrategicContextNavigation', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const currentRoot = root;
    if (currentRoot) {
      act(() => {
        currentRoot.unmount();
      });
    }
    root = null;
    host?.remove();
    host = null;
    openTarget = null;
    vi.clearAllMocks();
  });

  function mount(overrides: Partial<Parameters<typeof useStrategicContextNavigation>[0]> = {}) {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => {
      root?.render(<Harness {...overrides} />);
    });
    if (!openTarget) throw new Error('Expected navigation callback');
    return openTarget;
  }

  test('routes project, pipeline, and runtime targets through workbench tabs', () => {
    const handleTabChange = vi.fn();
    const setPipelineSection = vi.fn();
    const setProjectSection = vi.fn();
    const setRuntimeSection = vi.fn();
    const setShowArchive = vi.fn();
    const setViewMode = vi.fn();
    const open = mount({ handleTabChange, setPipelineSection, setProjectSection, setRuntimeSection, setShowArchive, setViewMode });

    act(() => {
      open({ surface: 'project', section: 'problem-model' } as Target);
      open({ surface: 'pipeline', section: 'board' } as Target);
      open({ surface: 'runtime', section: 'devices' } as Target);
    });

    expect(setProjectSection).toHaveBeenCalledWith('problem-model');
    expect(setPipelineSection).toHaveBeenCalledWith('board');
    expect(setViewMode).toHaveBeenCalledWith('kanban');
    expect(setShowArchive).toHaveBeenCalledWith(false);
    expect(setRuntimeSection).toHaveBeenCalledWith('devices');
    expect(handleTabChange).toHaveBeenCalledWith('project');
    expect(handleTabChange).toHaveBeenCalledWith('pipeline');
    expect(handleTabChange).toHaveBeenCalledWith('runtime');
  });

  test('routes settings targets through their canonical project and runtime homes', () => {
    const handleTabChange = vi.fn();
    const setProjectSection = vi.fn();
    const setProjectManageSection = vi.fn();
    const setRuntimeSection = vi.fn();
    const setRuntimeConfigSection = vi.fn();
    const open = mount({
      handleTabChange,
      setProjectSection,
      setProjectManageSection,
      setRuntimeSection,
      setRuntimeConfigSection,
    });

    act(() => {
      open({ surface: 'project', section: 'build-config' } as Target);
      open({ surface: 'runtime', section: 'provider-settings' } as Target);
      open({ surface: 'runtime', section: 'accounts' } as Target);
    });

    expect(setProjectSection).toHaveBeenCalledWith('manage');
    expect(setProjectManageSection).toHaveBeenCalledWith('build-config');
    expect(setRuntimeSection).toHaveBeenCalledWith('settings');
    expect(setRuntimeConfigSection).toHaveBeenCalledWith('provider-settings');
    expect(setRuntimeSection).toHaveBeenCalledWith('accounts');
    expect(handleTabChange.mock.calls.map(([tab]) => tab)).toEqual(['project', 'runtime', 'runtime']);
  });

  test('routes messages, tasks, and knowledge targets through their owning surfaces', () => {
    const handleTabChange = vi.fn();
    const openMessages = vi.fn();
    const selectPipelineTaskById = vi.fn();
    const setKnowledgeBrowserDirectionId = vi.fn();
    const setKnowledgeBrowserInitialDocId = vi.fn();
    const setPipelineSection = vi.fn();
    const setShowArchive = vi.fn();
    const setShowKnowledgeBrowser = vi.fn();
    const setViewMode = vi.fn();
    const open = mount({
      handleTabChange,
      openMessages,
      selectPipelineTaskById,
      setKnowledgeBrowserDirectionId,
      setKnowledgeBrowserInitialDocId,
      setPipelineSection,
      setShowArchive,
      setShowKnowledgeBrowser,
      setViewMode,
    });

    act(() => {
      open({ surface: 'messages', message_id: 'msg-1', action: 'reply' } as Target);
      open({ surface: 'task', task_id: 'task-1' } as Target);
      open({ surface: 'knowledge', document_id: 'doc-1', direction_id: 'dir-1' } as Target);
    });

    expect(openMessages).toHaveBeenCalledWith({ messageId: 'msg-1', action: 'reply' });
    expect(selectPipelineTaskById).toHaveBeenCalledWith('task-1');
    expect(setPipelineSection).toHaveBeenCalledWith('flow');
    expect(setViewMode).toHaveBeenCalledWith('pipeline');
    expect(setShowArchive).toHaveBeenCalledWith(false);
    expect(handleTabChange).toHaveBeenCalledWith('pipeline');
    expect(setKnowledgeBrowserInitialDocId).toHaveBeenCalledWith('doc-1');
    expect(setKnowledgeBrowserDirectionId).toHaveBeenCalledWith('dir-1');
    expect(setShowKnowledgeBrowser).toHaveBeenCalledWith(true);
    expect(handleTabChange).toHaveBeenCalledTimes(1);
  });

  test('pipeline targets preserve task specificity when a task id is present', () => {
    const handleTabChange = vi.fn();
    const selectPipelineTaskById = vi.fn();
    const setPipelineSection = vi.fn();
    const setShowArchive = vi.fn();
    const setViewMode = vi.fn();
    const open = mount({
      handleTabChange,
      selectPipelineTaskById,
      setPipelineSection,
      setShowArchive,
      setViewMode,
    });

    act(() => {
      open({ surface: 'pipeline', section: 'flow', task_id: 'task-flow-1' } as Target);
    });

    expect(setPipelineSection).toHaveBeenCalledWith('flow');
    expect(setViewMode).toHaveBeenCalledWith('pipeline');
    expect(setShowArchive).toHaveBeenCalledWith(false);
    expect(selectPipelineTaskById).toHaveBeenCalledWith('task-flow-1');
    expect(handleTabChange).toHaveBeenCalledWith('pipeline');
  });
});
