import { useCallback, useEffect, type RefObject } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import type { PrimaryTab } from './useUIState';
import { isEditableShortcutEvent } from './keyboardTargets';
import { useCommandPaletteShortcut } from './useCommandPaletteShortcut';
import { useWorkbenchShortcutRouter } from './useWorkbenchShortcutRouter';

interface UseOperatorKeyboardShortcutsOptions {
  activeTab: PrimaryTab;
  blockingModalOpen: boolean;
  cycleCurrentSection?: (direction: -1 | 1) => boolean;
  commandPaletteShortcutBlocked?: boolean;
  commandPaletteOpen: boolean;
  commands: WorkbenchCommand[];
  searchInputRef: RefObject<HTMLInputElement | null>;
  searchShortcutBlocked: boolean;
  selectedTaskCount: number;
  selectionMode: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
  setKnowledgeBrowserDirectionId: (directionId: string | null) => void;
  setKnowledgeBrowserInitialDocId: (documentId: string | null) => void;
  setResearchBrowserDirectionId: (directionId: string | null) => void;
  setShortcutHelpOpen?: (open: boolean) => void;
  setShowActivityFeed: (show: boolean) => void;
  setShowKnowledgeBrowser: (show: boolean) => void;
  setShowResearchBrowser: (show: boolean) => void;
  shortcutHelpOpen?: boolean;
  transientPanelOpen?: boolean;
}

export function useOperatorKeyboardShortcuts({
  activeTab,
  blockingModalOpen,
  cycleCurrentSection,
  commandPaletteShortcutBlocked = blockingModalOpen,
  commandPaletteOpen,
  commands,
  searchInputRef,
  searchShortcutBlocked,
  selectedTaskCount,
  selectionMode,
  setCommandPaletteOpen,
  setKnowledgeBrowserDirectionId,
  setKnowledgeBrowserInitialDocId,
  setResearchBrowserDirectionId,
  setShortcutHelpOpen,
  setShowActivityFeed,
  setShowKnowledgeBrowser,
  setShowResearchBrowser,
  shortcutHelpOpen = false,
  transientPanelOpen = false,
}: UseOperatorKeyboardShortcutsOptions) {
  const closeTransientTargets = useCallback(() => {
    setShowResearchBrowser(false);
    setShowKnowledgeBrowser(false);
    setShowActivityFeed(false);
    setResearchBrowserDirectionId(null);
    setKnowledgeBrowserDirectionId(null);
    setKnowledgeBrowserInitialDocId(null);
  }, [
    setKnowledgeBrowserDirectionId,
    setKnowledgeBrowserInitialDocId,
    setResearchBrowserDirectionId,
    setShowActivityFeed,
    setShowKnowledgeBrowser,
    setShowResearchBrowser,
  ]);

  const closeTransientWorkbenchPanels = useCallback(() => {
    closeTransientTargets();
  }, [closeTransientTargets]);

  const openCommandPalette = useCallback(() => {
    if (commandPaletteOpen || commandPaletteShortcutBlocked || shortcutHelpOpen) return;
    closeTransientWorkbenchPanels();
    setCommandPaletteOpen(true);
  }, [closeTransientWorkbenchPanels, commandPaletteOpen, commandPaletteShortcutBlocked, setCommandPaletteOpen, shortcutHelpOpen]);

  useCommandPaletteShortcut(openCommandPalette, !commandPaletteOpen && !commandPaletteShortcutBlocked && !shortcutHelpOpen);

  useEffect(() => {
    function handleSearchShortcut(event: KeyboardEvent) {
      if (event.key !== '/') return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      if (isEditableShortcutEvent(event)) return;
      if (blockingModalOpen) return;
      if (commandPaletteOpen || shortcutHelpOpen) return;
      if (searchShortcutBlocked) return;
      event.preventDefault();
      searchInputRef.current?.focus();
    }

    document.addEventListener('keydown', handleSearchShortcut);
    return () => document.removeEventListener('keydown', handleSearchShortcut);
  }, [blockingModalOpen, commandPaletteOpen, searchInputRef, searchShortcutBlocked, shortcutHelpOpen]);

  useEffect(() => {
    function consume(event: KeyboardEvent) {
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
    }

    function handleOperatorShortcut(event: KeyboardEvent) {
      if (event.defaultPrevented) return;

      if (event.key === 'Escape') {
        if (shortcutHelpOpen && setShortcutHelpOpen) {
          consume(event);
          setShortcutHelpOpen(false);
          return;
        }
        if (transientPanelOpen) {
          consume(event);
          closeTransientTargets();
        }
        return;
      }

      if (blockingModalOpen || commandPaletteOpen || shortcutHelpOpen) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isEditableShortcutEvent(event)) return;

      if (event.key === '?' && setShortcutHelpOpen) {
        consume(event);
        closeTransientWorkbenchPanels();
        setShortcutHelpOpen(true);
        return;
      }

      if ((event.key === '[' || event.key === ']') && cycleCurrentSection) {
        const handled = cycleCurrentSection(event.key === ']' ? 1 : -1);
        if (handled) {
          consume(event);
          closeTransientWorkbenchPanels();
        }
      }
    }

    document.addEventListener('keydown', handleOperatorShortcut, true);
    return () => document.removeEventListener('keydown', handleOperatorShortcut, true);
  }, [
    blockingModalOpen,
    closeTransientTargets,
    closeTransientWorkbenchPanels,
    commandPaletteOpen,
    cycleCurrentSection,
    setShortcutHelpOpen,
    shortcutHelpOpen,
    transientPanelOpen,
  ]);

  const shouldHandleWorkbenchShortcut = useCallback((commandId: string) => {
    if (blockingModalOpen) return false;
    if (
      commandId === 'workbench.subtab.previous'
      || commandId === 'workbench.subtab.next'
    ) {
      return activeTab === 'pipeline' || activeTab === 'project' || activeTab === 'runtime';
    }
    if (commandId === 'workbench.refresh') {
      if (selectionMode && selectedTaskCount > 0) return false;
      return true;
    }
    if (commandId === 'pipeline.archive') return activeTab === 'pipeline';
    return true;
  }, [
    activeTab,
    blockingModalOpen,
    selectedTaskCount,
    selectionMode,
  ]);

  useWorkbenchShortcutRouter(commands, {
    enabled: !commandPaletteOpen && !shortcutHelpOpen,
    onBeforeRun: closeTransientWorkbenchPanels,
    shouldHandleShortcut: shouldHandleWorkbenchShortcut,
  });

  return { openCommandPalette };
}
