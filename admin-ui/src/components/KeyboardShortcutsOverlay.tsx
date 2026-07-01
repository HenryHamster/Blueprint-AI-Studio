import { Keyboard, X } from 'lucide-react';
import { useEffect, useRef, type KeyboardEvent } from 'react';

type ShortcutRow = {
  keys: string[];
  label: string;
};

type ShortcutGroup = {
  title: string;
  rows: ShortcutRow[];
};

const SHORTCUT_GROUPS: ShortcutGroup[] = [
  {
    title: 'Navigate',
    rows: [
      { keys: ['1'], label: 'Chat' },
      { keys: ['2'], label: 'Pipeline' },
      { keys: ['3'], label: 'Project' },
      { keys: ['4'], label: 'Runtime' },
      { keys: ['['], label: 'Previous section' },
      { keys: [']'], label: 'Next section' },
    ],
  },
  {
    title: 'Operate',
    rows: [
      { keys: ['Ctrl/Cmd', 'K'], label: 'Command palette' },
      { keys: ['/'], label: 'Search or filter' },
      { keys: ['N'], label: 'New task' },
      { keys: ['R'], label: 'Refresh' },
    ],
  },
  {
    title: 'Panels',
    rows: [
      { keys: ['A'], label: 'Archive in Pipeline' },
      { keys: ['S'], label: 'Runtime settings' },
      { keys: ['?'], label: 'Keyboard shortcuts' },
      { keys: ['Esc'], label: 'Close panel' },
    ],
  },
];

interface KeyboardShortcutsOverlayProps {
  open: boolean;
  onClose: () => void;
}

function ShortcutKey({ children }: { children: string }) {
  return (
    <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded border border-workbench-border-strong bg-workbench-control px-1.5 font-mono text-ui-md font-semibold leading-none text-neutral-300 shadow-[inset_0_-1px_0_rgba(255,255,255,0.04)]">
      {children}
    </kbd>
  );
}

export function KeyboardShortcutsOverlay({ open, onClose }: KeyboardShortcutsOverlayProps) {
  const panelRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const timer = window.setTimeout(() => closeButtonRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(timer);
      restoreFocusRef.current?.focus();
      restoreFocusRef.current = null;
    };
  }, [open]);

  if (!open) return null;

  function focusableElements(): HTMLElement[] {
    if (!panelRef.current) return [];
    return Array.from(
      panelRef.current.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter(element => !element.hasAttribute('disabled'));
  }

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation?.();

    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }

    if (event.key !== 'Tab') return;

    const focusables = focusableElements();
    if (focusables.length === 0) return;
    const currentIndex = focusables.indexOf(document.activeElement as HTMLElement);
    const nextIndex = event.shiftKey
      ? (currentIndex <= 0 ? focusables.length - 1 : currentIndex - 1)
      : (currentIndex === -1 || currentIndex === focusables.length - 1 ? 0 : currentIndex + 1);
    event.preventDefault();
    focusables[nextIndex]?.focus();
  }

  return (
    <div
      className="fixed inset-0 z-[80] flex items-start justify-center bg-black/70 px-3 py-3 backdrop-blur-sm sm:items-center sm:py-5"
      onMouseDown={event => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="keyboard-shortcuts-title"
        className="grid max-h-[calc(100vh-24px)] w-[min(560px,calc(100vw-24px))] min-w-0 grid-rows-[auto_minmax(0,1fr)] overflow-hidden rounded-md border border-workbench-border bg-workbench-panel shadow-2xl shadow-black/40 sm:max-h-[calc(100vh-40px)]"
        onKeyDown={handleKeyDown}
      >
        <header className="flex min-w-0 items-center justify-between gap-2.5 border-b border-workbench-border bg-workbench-shell px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-workbench-border bg-workbench-control text-neutral-300">
              <Keyboard className="h-4 w-4" aria-hidden="true" />
            </span>
            <h2 id="keyboard-shortcuts-title" className="truncate text-sm font-semibold text-neutral-100">
              Keyboard shortcuts
            </h2>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label="Close keyboard shortcuts"
            className="workbench-focus flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md border border-workbench-border bg-workbench-control text-neutral-400 transition-colors hover:border-workbench-border-strong hover:text-neutral-100 md:min-h-8 md:min-w-8"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </header>

        <div className="min-h-0 overflow-y-auto bg-workbench-panel p-2 custom-scrollbar">
          <div className="grid gap-2 sm:grid-cols-3">
            {SHORTCUT_GROUPS.map(group => (
              <section key={group.title} className="min-w-0 rounded-md border border-workbench-border bg-workbench-layer/55 p-2">
                <h3 className="px-1 pb-1.5 text-ui-md font-semibold uppercase tracking-wide text-neutral-500">
                  {group.title}
                </h3>
                <div className="space-y-1">
                  {group.rows.map(row => (
                    <div key={`${group.title}-${row.label}`} className="flex min-h-8 min-w-0 items-center justify-between gap-2 rounded px-1.5 py-1 text-xs">
                      <span className="min-w-0 truncate font-medium text-neutral-200">{row.label}</span>
                      <span className="flex shrink-0 items-center gap-1">
                        {row.keys.map(key => (
                          <ShortcutKey key={key}>{key}</ShortcutKey>
                        ))}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
