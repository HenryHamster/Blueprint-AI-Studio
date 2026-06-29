import { useEffect } from 'react';
import { isEditableShortcutEvent } from './keyboardTargets';

export function useCommandPaletteShortcut(onOpen: () => void, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    const handleCommandShortcut = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.altKey || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k') return;
      if (isEditableShortcutEvent(event)) return;
      event.preventDefault();
      onOpen();
    };
    document.addEventListener('keydown', handleCommandShortcut, true);
    return () => document.removeEventListener('keydown', handleCommandShortcut, true);
  }, [enabled, onOpen]);
}
