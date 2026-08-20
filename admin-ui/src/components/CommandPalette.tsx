import { Command, Search, X } from 'lucide-react';
import type { KeyboardEvent } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';

export type WorkbenchCommand = {
  id: string;
  label: string;
  section?: string;
  detail?: string;
  keywords?: string[];
  disabled?: boolean;
  shortcut?: string;
  run: () => void;
};

interface CommandPaletteProps {
  open: boolean;
  commands: WorkbenchCommand[];
  onClose: () => void;
  query?: string;
  onQueryChange?: (query: string) => void;
  placeholder?: string;
  title?: string;
}

function firstEnabledIndex(commands: WorkbenchCommand[]): number {
  return commands.findIndex(command => !command.disabled);
}

function commandMatches(command: WorkbenchCommand, query: string): boolean {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;

  const searchable = [
    command.label,
    command.section ?? '',
    command.detail ?? '',
    ...(command.keywords ?? []),
  ].join(' ').toLowerCase();

  return terms.every(term => searchable.includes(term));
}

export function CommandPalette({
  open,
  commands,
  onClose,
  query,
  onQueryChange,
  placeholder = 'Search commands...',
  title = 'Command palette',
}: CommandPaletteProps) {
  const [internalQuery, setInternalQuery] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const clearButtonRef = useRef<HTMLButtonElement>(null);
  const activeCommandRef = useRef<HTMLButtonElement | null>(null);
  const focusActiveCommandRef = useRef(false);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const searchQuery = query ?? internalQuery;

  const filteredCommands = useMemo(
    () => commands.filter(command => commandMatches(command, searchQuery)),
    [commands, searchQuery],
  );

  const [activeIndex, setActiveIndex] = useState(0);
  const resolvedActiveIndex = useMemo(() => {
    if (
      activeIndex >= 0 &&
      activeIndex < filteredCommands.length &&
      !filteredCommands[activeIndex]?.disabled
    ) {
      return activeIndex;
    }
    return firstEnabledIndex(filteredCommands);
  }, [activeIndex, filteredCommands]);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const timer = window.setTimeout(() => {
      inputRef.current?.focus();
    }, 0);
    return () => {
      window.clearTimeout(timer);
      restoreFocusRef.current?.focus();
      restoreFocusRef.current = null;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    activeCommandRef.current?.scrollIntoView?.({ block: 'nearest' });
    if (focusActiveCommandRef.current) {
      activeCommandRef.current?.focus();
      focusActiveCommandRef.current = false;
    }
  }, [open, resolvedActiveIndex]);

  if (!open) return null;

  function updateQuery(nextQuery: string) {
    if (query === undefined) {
      setInternalQuery(nextQuery);
    }
    onQueryChange?.(nextQuery);
  }

  function executeCommand(command: WorkbenchCommand | undefined) {
    if (!command || command.disabled) return;
    command.run();
  }

  function moveActive(direction: 1 | -1) {
    if (filteredCommands.length === 0) return;

    setActiveIndex(() => {
      const start = resolvedActiveIndex >= 0 ? resolvedActiveIndex : (direction === 1 ? -1 : filteredCommands.length);
      for (let offset = 1; offset <= filteredCommands.length; offset += 1) {
        const nextIndex = (start + direction * offset + filteredCommands.length) % filteredCommands.length;
        if (!filteredCommands[nextIndex]?.disabled) {
          return nextIndex;
        }
      }
      return resolvedActiveIndex;
    });
  }

  function moveToBoundary(boundary: 'first' | 'last', { focusCommand = false }: { focusCommand?: boolean } = {}) {
    const enabledCommands = filteredCommands
      .map((command, index) => ({ command, index }))
      .filter(({ command }) => !command.disabled);
    const next = boundary === 'first' ? enabledCommands[0] : enabledCommands.at(-1);
    if (!next) return;
    if (focusCommand && next.index === resolvedActiveIndex) {
      // Already at boundary — index won't change so the clearing effect won't re-run.
      // Focus directly without arming the ref flag to avoid stranding it.
      window.setTimeout(() => activeCommandRef.current?.focus(), 0);
      return;
    }
    if (focusCommand) focusActiveCommandRef.current = true;
    setActiveIndex(next.index);
    if (focusCommand && next.index === resolvedActiveIndex) {
      // No-op boundary: resolvedActiveIndex won't change, so the effect that
      // clears the flag never re-runs. Clear it here and focus directly, or the
      // stale flag would later yank focus out of the search input mid-typing.
      focusActiveCommandRef.current = false;
      window.setTimeout(() => activeCommandRef.current?.focus(), 0);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation?.();
    const target = event.target;
    const textEntryTarget = target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      (target instanceof HTMLElement && target.isContentEditable);

    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }

    if (event.key === 'Tab') {
      const focusables = ([
        inputRef.current,
        clearButtonRef.current,
        activeCommandRef.current,
        closeButtonRef.current,
      ] as Array<HTMLElement | null>).filter((element): element is HTMLElement => element !== null);
      if (focusables.length === 0) return;
      const currentIndex = focusables.indexOf(document.activeElement as HTMLElement);
      const nextIndex = event.shiftKey
        ? (currentIndex <= 0 ? focusables.length - 1 : currentIndex - 1)
        : (currentIndex === -1 || currentIndex === focusables.length - 1 ? 0 : currentIndex + 1);
      event.preventDefault();
      focusables[nextIndex]?.focus();
      return;
    }

    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveActive(1);
      return;
    }

    if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveActive(-1);
      return;
    }

    if (event.key === 'Home') {
      if (textEntryTarget) return;
      event.preventDefault();
      moveToBoundary('first', { focusCommand: true });
      return;
    }

    if (event.key === 'End') {
      if (textEntryTarget) return;
      event.preventDefault();
      moveToBoundary('last', { focusCommand: true });
      return;
    }

    if (event.key === 'Enter') {
      if (event.target instanceof HTMLButtonElement) return;
      event.preventDefault();
      executeCommand(filteredCommands[resolvedActiveIndex]);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[80] flex items-start justify-center bg-black/70 px-3 py-3 backdrop-blur-sm sm:items-center sm:py-5"
      onMouseDown={event => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="command-palette-title"
        className="grid max-h-[calc(100vh-24px)] w-[min(680px,calc(100vw-24px))] min-w-0 grid-rows-[auto_auto_minmax(0,1fr)] overflow-hidden rounded-md border border-workbench-border bg-workbench-panel shadow-2xl shadow-black/40 sm:max-h-[calc(100vh-40px)]"
        onKeyDown={handleKeyDown}
      >
        <header className="flex min-w-0 items-center justify-between gap-2.5 border-b border-workbench-border bg-workbench-shell px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-neutral-800 bg-workbench-shell text-neutral-300">
              <Command className="h-4 w-4" aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <h2 id="command-palette-title" className="truncate text-sm font-semibold text-neutral-100">
                {title}
              </h2>
              <p className="truncate text-ui-md text-neutral-500">
                {filteredCommands.length} of {commands.length} commands
              </p>
            </div>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label="Close command palette"
            className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md border border-neutral-800 bg-workbench-shell text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-100 md:min-h-8 md:min-w-8"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </header>

        <div className="flex min-w-0 items-center gap-2 border-b border-workbench-border bg-workbench-base px-3 py-1.5">
          <Search className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            value={searchQuery}
            onChange={event => updateQuery(event.target.value)}
            placeholder={placeholder}
            aria-label="Search commands"
            className="min-h-11 min-w-0 flex-1 bg-transparent text-base text-neutral-100 outline-none placeholder:text-neutral-600 md:min-h-8 md:text-sm"
          />
          {searchQuery && (
            <button
              ref={clearButtonRef}
              type="button"
              onClick={() => updateQuery('')}
              aria-label="Clear command search"
              className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md border border-neutral-800 bg-workbench-shell text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-100 md:min-h-8 md:min-w-8"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
        </div>

        <div className="min-h-0 overflow-y-auto bg-workbench-panel p-1 custom-scrollbar">
          {filteredCommands.length === 0 ? (
            <div className="px-3 py-8 text-center text-sm text-neutral-500">
              No commands found
            </div>
          ) : (
            <div className="space-y-0.5" role="list" aria-label="Available commands">
              {filteredCommands.map((command, index) => {
                const active = index === resolvedActiveIndex;
                const previousSection = filteredCommands[index - 1]?.section;
                const showSection = command.section && command.section !== previousSection;
                return (
                  <div key={command.id} className="min-w-0">
                    {showSection && (
                      <div className="px-2 pb-0.5 pt-1.5 text-ui-md font-semibold uppercase tracking-wide text-neutral-500">
                        {command.section}
                      </div>
                    )}
                    <button
                      ref={active ? activeCommandRef : undefined}
                      type="button"
                      disabled={command.disabled}
                      aria-disabled={command.disabled || undefined}
                      aria-current={active ? 'true' : undefined}
                      tabIndex={active ? 0 : -1}
                      onMouseEnter={() => {
                        if (!command.disabled) setActiveIndex(index);
                      }}
                      onClick={() => executeCommand(command)}
                      className={`flex min-h-11 w-full min-w-0 items-center gap-2.5 rounded-md border px-2.5 py-1.5 text-left transition-colors md:min-h-9 ${
                        command.disabled
                          ? 'cursor-not-allowed border-transparent text-neutral-500 opacity-65'
                          : active
                            ? 'border-cyan-400/40 bg-cyan-400/10 text-neutral-50 shadow-[inset_2px_0_0_rgba(34,211,238,0.75)]'
                            : 'border-transparent text-neutral-200 hover:bg-workbench-hover/70'
                      }`}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{command.label}</span>
                        {command.detail && (
                          <span className="mt-0.5 block truncate text-xs text-neutral-500">
                            {command.detail}
                          </span>
                        )}
                      </span>
                      {command.shortcut && (
                        <kbd className="hidden shrink-0 rounded border border-neutral-700 bg-workbench-shell/80 px-1.5 py-0.5 font-mono text-ui-md text-neutral-500 sm:inline-block">
                          {command.shortcut}
                        </kbd>
                      )}
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
