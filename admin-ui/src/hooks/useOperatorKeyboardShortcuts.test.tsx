import { afterEach, describe, expect, test, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { useRef } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import type { PrimaryTab } from './useUIState';
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
  activeTab = 'chat',
  commandPaletteOpen = false,
  cycleCurrentSection,
  commands = COMMANDS,
  commandPaletteShortcutBlocked,
  searchShortcutBlocked = false,
  setCommandPaletteOpen,
  setShortcutHelpOpen,
  setShowActivityFeed = vi.fn(),
  setShowKnowledgeBrowser = vi.fn(),
  setShowResearchBrowser = vi.fn(),
  shortcutHelpOpen = false,
  transientPanelOpen = false,
}: {
  blockingModalOpen: boolean;
  activeTab?: PrimaryTab;
  commandPaletteOpen?: boolean;
  cycleCurrentSection?: (direction: -1 | 1) => boolean;
  commands?: WorkbenchCommand[];
  commandPaletteShortcutBlocked?: boolean;
  searchShortcutBlocked?: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
  setShortcutHelpOpen?: (open: boolean) => void;
  setShowActivityFeed?: (show: boolean) => void;
  setShowKnowledgeBrowser?: (show: boolean) => void;
  setShowResearchBrowser?: (show: boolean) => void;
  shortcutHelpOpen?: boolean;
  transientPanelOpen?: boolean;
}) {
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  useOperatorKeyboardShortcuts({
    activeTab,
    blockingModalOpen,
    cycleCurrentSection,
    commandPaletteShortcutBlocked,
    commandPaletteOpen,
    commands,
    searchInputRef,
    searchShortcutBlocked,
    selectedTaskCount: 0,
    selectionMode: false,
    setCommandPaletteOpen,
    setKnowledgeBrowserDirectionId: vi.fn(),
    setKnowledgeBrowserInitialDocId: vi.fn(),
    setResearchBrowserDirectionId: vi.fn(),
    setShortcutHelpOpen,
    setShowActivityFeed,
    setShowKnowledgeBrowser,
    setShowResearchBrowser,
    shortcutHelpOpen,
    transientPanelOpen,
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
      activeTab?: PrimaryTab;
      commands?: WorkbenchCommand[];
      commandPaletteShortcutBlocked?: boolean;
      cycleCurrentSection?: (direction: -1 | 1) => boolean;
      searchShortcutBlocked?: boolean;
      setShortcutHelpOpen?: (open: boolean) => void;
      setShowActivityFeed?: (show: boolean) => void;
      setShowKnowledgeBrowser?: (show: boolean) => void;
      setShowResearchBrowser?: (show: boolean) => void;
      shortcutHelpOpen?: boolean;
      transientPanelOpen?: boolean;
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
          activeTab={options.activeTab}
          blockingModalOpen={blockingModalOpen}
          commandPaletteOpen={commandPaletteOpen}
          commands={options.commands}
          commandPaletteShortcutBlocked={options.commandPaletteShortcutBlocked}
          cycleCurrentSection={options.cycleCurrentSection}
          searchShortcutBlocked={options.searchShortcutBlocked}
          setCommandPaletteOpen={setCommandPaletteOpen}
          setShortcutHelpOpen={options.setShortcutHelpOpen}
          setShowActivityFeed={options.setShowActivityFeed}
          setShowKnowledgeBrowser={options.setShowKnowledgeBrowser}
          setShowResearchBrowser={options.setShowResearchBrowser}
          shortcutHelpOpen={options.shortcutHelpOpen}
          transientPanelOpen={options.transientPanelOpen}
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

  test('does not focus search when the current surface blocks search shortcuts', () => {
    mount(false, false, { searchShortcutBlocked: true });
    const searchInput = host?.querySelector<HTMLInputElement>('[aria-label="task search"]');
    if (!searchInput) throw new Error('Expected task search input');

    const event = dispatchShortcut(document, { key: '/', ctrlKey: false });

    expect(searchInput).not.toHaveFocus();
    expect(event.defaultPrevented).toBe(false);
  });

  test('does not focus search for modified slash chords', () => {
    mount();
    const searchInput = host?.querySelector<HTMLInputElement>('[aria-label="task search"]');
    if (!searchInput) throw new Error('Expected task search input');

    const event = dispatchShortcut(document, { key: '/', ctrlKey: true });

    expect(searchInput).not.toHaveFocus();
    expect(event.defaultPrevented).toBe(false);
  });

  test('opens shortcut help with question mark outside editable fields', () => {
    const setShortcutHelpOpen = vi.fn();
    mount(false, false, { setShortcutHelpOpen });

    const event = dispatchShortcut(document, { key: '?', ctrlKey: false, shiftKey: true });

    expect(setShortcutHelpOpen).toHaveBeenCalledWith(true);
    expect(event.defaultPrevented).toBe(true);
  });

  test('does not open shortcut help from the Strategic composer', () => {
    const setShortcutHelpOpen = vi.fn();
    mount(false, false, { setShortcutHelpOpen });
    const composer = host?.querySelector<HTMLTextAreaElement>('[aria-label="strategic composer"]');
    if (!composer) throw new Error('Expected Strategic composer textarea');

    const event = dispatchShortcut(composer, { key: '?', ctrlKey: false, shiftKey: true });

    expect(setShortcutHelpOpen).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  test('does not open shortcut help over blocking modals', () => {
    const blockedHelpOpen = vi.fn();
    mount(true, false, { setShortcutHelpOpen: blockedHelpOpen });

    const blockedEvent = dispatchShortcut(document, { key: '?', ctrlKey: false, shiftKey: true });

    expect(blockedHelpOpen).not.toHaveBeenCalled();
    expect(blockedEvent.defaultPrevented).toBe(false);
  });

  test('does not open shortcut help while command palette is open', () => {
    const paletteHelpOpen = vi.fn();
    mount(false, true, { setShortcutHelpOpen: paletteHelpOpen });

    const paletteEvent = dispatchShortcut(document, { key: '?', ctrlKey: false, shiftKey: true });

    expect(paletteHelpOpen).not.toHaveBeenCalled();
    expect(paletteEvent.defaultPrevented).toBe(false);
  });

  test('cycles current workbench section with bracket shortcuts', () => {
    const cycleCurrentSection = vi.fn(() => true);
    mount(false, false, { cycleCurrentSection });

    const nextEvent = dispatchShortcut(document, { key: ']', ctrlKey: false });
    const previousEvent = dispatchShortcut(document, { key: '[', ctrlKey: false });

    expect(cycleCurrentSection).toHaveBeenNthCalledWith(1, 1);
    expect(cycleCurrentSection).toHaveBeenNthCalledWith(2, -1);
    expect(nextEvent.defaultPrevented).toBe(true);
    expect(previousEvent.defaultPrevented).toBe(true);
  });

  test('leaves bracket shortcuts alone when the current surface has no sections', () => {
    const cycleCurrentSection = vi.fn(() => false);
    mount(false, false, { cycleCurrentSection });

    const event = dispatchShortcut(document, { key: ']', ctrlKey: false });

    expect(cycleCurrentSection).toHaveBeenCalledWith(1);
    expect(event.defaultPrevented).toBe(false);
  });

  test('closes shortcut help with Escape before legacy shortcuts handle it', () => {
    const setShortcutHelpOpen = vi.fn();
    mount(false, false, { setShortcutHelpOpen, shortcutHelpOpen: true });

    const event = dispatchShortcut(document, { key: 'Escape', ctrlKey: false });

    expect(setShortcutHelpOpen).toHaveBeenCalledWith(false);
    expect(event.defaultPrevented).toBe(true);
  });

  test('closes transient workbench panels with Escape', () => {
    const setShowActivityFeed = vi.fn();
    const setShowKnowledgeBrowser = vi.fn();
    const setShowResearchBrowser = vi.fn();
    mount(false, false, {
      setShowActivityFeed,
      setShowKnowledgeBrowser,
      setShowResearchBrowser,
      transientPanelOpen: true,
    });

    const event = dispatchShortcut(document, { key: 'Escape', ctrlKey: false });

    expect(setShowActivityFeed).toHaveBeenCalledWith(false);
    expect(setShowKnowledgeBrowser).toHaveBeenCalledWith(false);
    expect(setShowResearchBrowser).toHaveBeenCalledWith(false);
    expect(event.defaultPrevented).toBe(true);
  });

  test('does not swallow subtab shortcuts on chat', () => {
    const previousSubtab = vi.fn();
    mount(false, false, {
      activeTab: 'chat',
      commands: [
        ...COMMANDS,
        { id: 'workbench.subtab.previous', label: 'Previous subtab', run: previousSubtab },
      ],
    });

    const event = dispatchShortcut(document, { key: '[', ctrlKey: false });

    expect(previousSubtab).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

});
