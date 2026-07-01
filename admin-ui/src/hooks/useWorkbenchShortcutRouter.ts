import { useEffect } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import { isEditableShortcutEvent } from './keyboardTargets';

export const WORKBENCH_SHORTCUT_COMMAND_ID_BY_KEY: Readonly<Record<string, string>> = {
  '1': 'strategic.focus',
  '2': 'pipeline.flow',
  '3': 'project.update',
  '4': 'runtime.health',
  a: 'pipeline.archive',
  n: 'task.new',
  r: 'workbench.refresh',
  s: 'runtime.settings.general',
  '[': 'workbench.subtab.previous',
  ']': 'workbench.subtab.next',
  '?': 'workbench.shortcuts.open',
};

interface UseWorkbenchShortcutRouterOptions {
  enabled?: boolean;
  onBeforeRun?: (commandId: string, command: WorkbenchCommand) => void;
  shouldHandleShortcut?: (commandId: string, event: KeyboardEvent) => boolean;
}

export function useWorkbenchShortcutRouter(
  commands: WorkbenchCommand[],
  {
    enabled = true,
    onBeforeRun,
    shouldHandleShortcut,
  }: UseWorkbenchShortcutRouterOptions = {},
) {
  useEffect(() => {
    if (!enabled) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isEditableShortcutEvent(event)) return;

      const commandId = WORKBENCH_SHORTCUT_COMMAND_ID_BY_KEY[event.key.toLowerCase()];
      if (!commandId) return;
      if (shouldHandleShortcut && !shouldHandleShortcut(commandId, event)) return;

      const command = commands.find(candidate => candidate.id === commandId);
      if (!command) return;

      event.preventDefault();
      if (command.disabled) return;
      onBeforeRun?.(commandId, command);
      command.run();
    };

    document.addEventListener('keydown', handleKeyDown, true);
    return () => document.removeEventListener('keydown', handleKeyDown, true);
  }, [commands, enabled, onBeforeRun, shouldHandleShortcut]);
}
