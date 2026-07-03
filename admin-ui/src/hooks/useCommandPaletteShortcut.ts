import { useEffect } from 'react';

export function useCommandPaletteShortcut(onOpen: () => void, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    const handleCommandShortcut = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.altKey || event.shiftKey || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k') return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      onOpen();
    };
    document.addEventListener('keydown', handleCommandShortcut, true);
    return () => document.removeEventListener('keydown', handleCommandShortcut, true);
  }, [enabled, onOpen]);
}
