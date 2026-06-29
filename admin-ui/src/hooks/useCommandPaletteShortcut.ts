import { useEffect } from 'react';

function isEditableShortcutTarget(target: EventTarget | Element | null): boolean {
  const element = target instanceof Element ? target : document.activeElement;
  if (!(element instanceof Element)) return false;
  const tagName = element.tagName.toLowerCase();
  if (tagName === 'input' || tagName === 'textarea' || tagName === 'select') return true;
  const editable = element.closest<HTMLElement>('[contenteditable]');
  return Boolean(editable && editable.contentEditable !== 'false');
}

export function useCommandPaletteShortcut(onOpen: () => void, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    const handleCommandShortcut = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'k') return;
      if (isEditableShortcutTarget(event.target)) return;
      event.preventDefault();
      onOpen();
    };
    document.addEventListener('keydown', handleCommandShortcut);
    return () => document.removeEventListener('keydown', handleCommandShortcut);
  }, [enabled, onOpen]);
}
