import { afterEach, describe, expect, test, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { useRef } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import { useOperatorKeyboardShortcuts } from './useOperatorKeyboardShortcuts';

const COMMANDS: WorkbenchCommand[] = [
  { id: 'strategic.focus', label: 'Strategic', run: vi.fn() },
];

function dispatchShortcut(target: EventTarget, init: KeyboardEventInit = {}) {
  const event = new KeyboardEvent('keydown', {
    key: 'k',
    ctrlKey: true,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  target.dispatchEvent(event);
  return event;
}

function Harness({
  blockingModalOpen,
  commandPaletteOpen = false,
  commands = COMMANDS,
  commandPaletteShortcutBlocked,
  showSettings = false,
  setCommandPaletteOpen,
  toggleSettings = vi.fn(),
}: {
  blockingModalOpen: boolean;
  commandPaletteOpen?: boolean;
  commands?: WorkbenchCommand[];
  commandPaletteShortcutBlocked?: boolean;
  showSettings?: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
  toggleSettings?: () => void;
}) {
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  useOperatorKeyboardShortcuts({
    activeTab: 'chat',
    blockingModalOpen,
    commandPaletteShortcutBlocked,
    commandPaletteOpen,
    commands,
    searchInputRef,
    searchShortcutBlocked: false,
    selectedTaskCount: 0,
    selectionMode: false,
    setCommandPaletteOpen,
    setKnowledgeBrowserDirectionId: vi.fn(),
    setKnowledgeBrowserInitialDocId: vi.fn(),
    setResearchBrowserDirectionId: vi.fn(),
    setShowActivityFeed: vi.fn(),
    setShowAlertsPopover: vi.fn(),
    setShowKnowledgeBrowser: vi.fn(),
    setShowResearchBrowser: vi.fn(),
    showSettings,
    toggleSettings,
  });

  return (
    <>
      <input ref={searchInputRef} aria-label="task search" />
      <textarea aria-label="strategic composer" />
    </>
  );
}

describe('useOperatorKeyboardShortcuts', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
    }
    root = null;
    host?.remove();
    host = null;
  });

  function mount(
    blockingModalOpen = false,
    commandPaletteOpen = false,
    options: {
      commands?: WorkbenchCommand[];
      commandPaletteShortcutBlocked?: boolean;
      showSettings?: boolean;
      toggleSettings?: () => void;
    } = {},
  ) {
    const setCommandPaletteOpen = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    act(() => {
      currentRoot.render(
        <Harness
          blockingModalOpen={blockingModalOpen}
          commandPaletteOpen={commandPaletteOpen}
          commands={options.commands}
          commandPaletteShortcutBlocked={options.commandPaletteShortcutBlocked}
          showSettings={options.showSettings}
          setCommandPaletteOpen={setCommandPaletteOpen}
          toggleSettings={options.toggleSettings}
        />,
      );
    });
    return setCommandPaletteOpen;
  }

  test('opens the command palette from the Strategic composer', () => {
    const setCommandPaletteOpen = mount();
    const composer = host?.querySelector<HTMLTextAreaElement>('[aria-label="strategic composer"]');
    if (!composer) throw new Error('Expected Strategic composer textarea');

    const event = dispatchShortcut(composer);

    expect(setCommandPaletteOpen).toHaveBeenCalledWith(true);
    expect(event.defaultPrevented).toBe(true);
  });

  test('does not open the command palette over a blocking modal', () => {
    const setCommandPaletteOpen = mount(true);

    const event = dispatchShortcut(document);

    expect(setCommandPaletteOpen).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  test('does not reopen the command palette while it is already open', () => {
    const setCommandPaletteOpen = mount(false, true);

    const event = dispatchShortcut(document);

    expect(setCommandPaletteOpen).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  test('does not run single-key workbench shortcuts over a blocking modal', () => {
    const openPipeline = vi.fn();
    mount(true, false, {
      commands: [
        ...COMMANDS,
        { id: 'pipeline.flow', label: 'Pipeline', run: openPipeline },
      ],
    });

    const event = dispatchShortcut(document, { key: '2', ctrlKey: false });

    expect(openPipeline).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  test('can open the command palette when workbench shortcuts are blocked by an inspection drawer', () => {
    const openPipeline = vi.fn();
    const setCommandPaletteOpen = mount(true, false, {
      commandPaletteShortcutBlocked: false,
      commands: [
        ...COMMANDS,
        { id: 'pipeline.flow', label: 'Pipeline', run: openPipeline },
      ],
    });

    const paletteEvent = dispatchShortcut(document);
    const routeEvent = dispatchShortcut(document, { key: '2', ctrlKey: false });

    expect(setCommandPaletteOpen).toHaveBeenCalledWith(true);
    expect(paletteEvent.defaultPrevented).toBe(true);
    expect(openPipeline).not.toHaveBeenCalled();
    expect(routeEvent.defaultPrevented).toBe(false);
  });

  test('does not focus search over a blocking modal', () => {
    mount(true);
    const searchInput = host?.querySelector<HTMLInputElement>('[aria-label="task search"]');
    if (!searchInput) throw new Error('Expected task search input');

    const event = dispatchShortcut(document, { key: '/', ctrlKey: false });

    expect(searchInput).not.toHaveFocus();
    expect(event.defaultPrevented).toBe(false);
  });

  test('closes Settings with S without running the open Settings command underneath', () => {
    const toggleSettings = vi.fn();
    const openSettings = vi.fn();
    mount(false, false, {
      commands: [
        ...COMMANDS,
        { id: 'settings.general', label: 'Settings', run: openSettings },
      ],
      showSettings: true,
      toggleSettings,
    });

    const event = dispatchShortcut(document, { key: 's', ctrlKey: false });

    expect(toggleSettings).toHaveBeenCalledOnce();
    expect(openSettings).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
  });
});
