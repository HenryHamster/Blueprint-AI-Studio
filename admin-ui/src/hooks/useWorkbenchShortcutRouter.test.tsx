import { afterEach, describe, expect, test, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { WorkbenchCommand } from '../components/CommandPalette';
import { useWorkbenchShortcutRouter } from './useWorkbenchShortcutRouter';

function Harness({
  commands,
  enabled,
  onBeforeRun,
  shouldHandleShortcut,
}: {
  commands: WorkbenchCommand[];
  enabled?: boolean;
  onBeforeRun?: (commandId: string, command: WorkbenchCommand) => void;
  shouldHandleShortcut?: (commandId: string, event: KeyboardEvent) => boolean;
}) {
  useWorkbenchShortcutRouter(commands, { enabled, onBeforeRun, shouldHandleShortcut });
  return (
    <>
      <input aria-label="task title" />
      <textarea aria-label="strategic composer" />
      <select aria-label="status filter">
        <option>Queued</option>
      </select>
      <div role="textbox" aria-label="role editor" tabIndex={0}>
        Role editor
      </div>
      <div aria-label="rich editor" contentEditable suppressContentEditableWarning tabIndex={0}>
        <span>Editable draft</span>
      </div>
      <div data-shortcut-scope="editor" aria-label="embedded editor" tabIndex={0}>
        Embedded editor
      </div>
    </>
  );
}

function makeCommands(overrides: Partial<WorkbenchCommand>[] = []): WorkbenchCommand[] {
  const commandIds = [
    'strategic.focus',
    'pipeline.flow',
    'project.update',
    'runtime.health',
    'workbench.refresh',
    'task.new',
    'settings.general',
    'pipeline.archive',
  ];

  return commandIds.map((id, index) => {
    const run = vi.fn();
    return {
      id,
      label: id,
      run,
      ...overrides[index],
    };
  });
}

function dispatchShortcut(key: string, init: KeyboardEventInit = {}) {
  return dispatchShortcutTo(document, key, init);
}

function dispatchShortcutTo(target: EventTarget, key: string, init: KeyboardEventInit = {}) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  target.dispatchEvent(event);
  return event;
}

describe('useWorkbenchShortcutRouter', () => {
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
  });

  function mount(
    commands = makeCommands(),
    options: {
      enabled?: boolean;
      onBeforeRun?: (commandId: string, command: WorkbenchCommand) => void;
      shouldHandleShortcut?: (commandId: string, event: KeyboardEvent) => boolean;
    } = {},
  ) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    act(() => {
      currentRoot.render(<Harness commands={commands} {...options} />);
    });
    return commands;
  }

  test('routes main workbench shortcuts to their commands', () => {
    const commands = mount();

    dispatchShortcut('1');
    dispatchShortcut('2');
    dispatchShortcut('3');
    dispatchShortcut('4');
    dispatchShortcut('r');
    dispatchShortcut('n');
    dispatchShortcut('s');
    dispatchShortcut('a');

    expect(commands.find(command => command.id === 'strategic.focus')?.run).toHaveBeenCalledOnce();
    expect(commands.find(command => command.id === 'pipeline.flow')?.run).toHaveBeenCalledOnce();
    expect(commands.find(command => command.id === 'project.update')?.run).toHaveBeenCalledOnce();
    expect(commands.find(command => command.id === 'runtime.health')?.run).toHaveBeenCalledOnce();
    expect(commands.find(command => command.id === 'workbench.refresh')?.run).toHaveBeenCalledOnce();
    expect(commands.find(command => command.id === 'task.new')?.run).toHaveBeenCalledOnce();
    expect(commands.find(command => command.id === 'settings.general')?.run).toHaveBeenCalledOnce();
    expect(commands.find(command => command.id === 'pipeline.archive')?.run).toHaveBeenCalledOnce();
  });

  test('runs the pre-run hook before successful shortcuts', () => {
    const onBeforeRun = vi.fn();
    const commands = mount(makeCommands(), { onBeforeRun });

    dispatchShortcut('2');

    const pipelineCommand = commands.find(command => command.id === 'pipeline.flow');
    expect(onBeforeRun).toHaveBeenCalledWith('pipeline.flow', pipelineCommand);
    expect(pipelineCommand?.run).toHaveBeenCalledOnce();
  });

  test('prevents a disabled shortcut command without running it', () => {
    const commands = mount(makeCommands([
      {},
      {},
      {},
      {},
      {},
      { disabled: true },
    ]));

    const event = dispatchShortcut('n');

    expect(commands.find(command => command.id === 'task.new')?.run).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
  });

  test('can defer a shortcut to legacy selection-mode handling', () => {
    const commands = mount(makeCommands(), {
      shouldHandleShortcut: commandId => commandId !== 'workbench.refresh',
    });

    const event = dispatchShortcut('r');

    expect(commands.find(command => command.id === 'workbench.refresh')?.run).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  test('does not hijack text inputs or editable regions', () => {
    const commands = mount();
    host?.querySelector<HTMLInputElement>('input')?.focus();
    const inputEvent = dispatchShortcut('n');

    host?.querySelector<HTMLTextAreaElement>('textarea')?.focus();
    const textareaEvent = dispatchShortcut('r');

    host?.querySelector<HTMLSelectElement>('select')?.focus();
    const selectEvent = dispatchShortcut('s');

    host?.querySelector<HTMLElement>('[aria-label="role editor"]')?.focus();
    const roleEvent = dispatchShortcut('a');

    const richEditorChild = host?.querySelector<HTMLElement>('[aria-label="rich editor"] span');
    if (!richEditorChild) throw new Error('Expected nested rich editor child');
    const editorEvent = dispatchShortcutTo(richEditorChild, 'r');

    host?.querySelector<HTMLElement>('[aria-label="embedded editor"]')?.focus();
    const embeddedEditorEvent = dispatchShortcut('n');

    expect(commands.find(command => command.id === 'task.new')?.run).not.toHaveBeenCalled();
    expect(commands.find(command => command.id === 'workbench.refresh')?.run).not.toHaveBeenCalled();
    expect(commands.find(command => command.id === 'settings.general')?.run).not.toHaveBeenCalled();
    expect(commands.find(command => command.id === 'pipeline.archive')?.run).not.toHaveBeenCalled();
    expect(inputEvent.defaultPrevented).toBe(false);
    expect(textareaEvent.defaultPrevented).toBe(false);
    expect(selectEvent.defaultPrevented).toBe(false);
    expect(roleEvent.defaultPrevented).toBe(false);
    expect(editorEvent.defaultPrevented).toBe(false);
    expect(embeddedEditorEvent.defaultPrevented).toBe(false);
  });

  test('ignores modified browser chords', () => {
    const commands = mount();

    dispatchShortcut('r', { ctrlKey: true });
    dispatchShortcut('s', { metaKey: true });

    expect(commands.find(command => command.id === 'workbench.refresh')?.run).not.toHaveBeenCalled();
    expect(commands.find(command => command.id === 'settings.general')?.run).not.toHaveBeenCalled();
  });
});
