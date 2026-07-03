const NON_TEXT_INPUT_TYPES = new Set([
  'button',
  'checkbox',
  'color',
  'file',
  'hidden',
  'image',
  'radio',
  'range',
  'reset',
  'submit',
]);

const EDITOR_SELECTOR = [
  'textarea',
  'select',
  '[role="textbox"]',
  '[data-lexical-editor]',
  '[data-shortcut-scope="editor"]',
  '.cm-editor',
  '.monaco-editor',
  '.ProseMirror',
].join(',');

function elementFromTarget(target: EventTarget | null): Element | null {
  if (target instanceof Element) return target;
  return null;
}

function hasEditableAncestor(element: Element): boolean {
  let current: Element | null = element;
  while (current) {
    const value = current.getAttribute('contenteditable');
    if (value !== null) return value.toLowerCase() !== 'false';
    current = current.parentElement;
  }
  return false;
}

export function isEditableShortcutTarget(target: EventTarget | null): boolean {
  const element = elementFromTarget(target);
  if (!element) return false;

  const input = element.closest('input');
  if (input instanceof HTMLInputElement) {
    return !NON_TEXT_INPUT_TYPES.has(input.type.toLowerCase());
  }

  if (hasEditableAncestor(element)) return true;

  return element.closest(EDITOR_SELECTOR) !== null;
}

export function isEditableShortcutEvent(event: KeyboardEvent): boolean {
  return (
    isEditableShortcutTarget(event.target) ||
    isEditableShortcutTarget(document.activeElement)
  );
}
