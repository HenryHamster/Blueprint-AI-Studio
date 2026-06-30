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
  setCommandPaletteOpen,
}: {
  blockingModalOpen: boolean;
  commandPaletteOpen?: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
}) {
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  useOperatorKeyboardShortcuts({
    activeTab: 'chat',
    blockingModalOpen,
    commandPaletteOpen,
    commands: COMMANDS,
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
    showSettings: false,
    toggleSettings: vi.fn(),
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

  function mount(blockingModalOpen = false, commandPaletteOpen = false) {
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
          setCommandPaletteOpen={setCommandPaletteOpen}
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

  test('does not focus search over a blocking modal', () => {
    mount(true);
    const searchInput = host?.querySelector<HTMLInputElement>('[aria-label="task search"]');
    if (!searchInput) throw new Error('Expected task search input');

    const event = dispatchShortcut(document, { key: '/', ctrlKey: false });

    expect(searchInput).not.toHaveFocus();
    expect(event.defaultPrevented).toBe(false);
  });
});
