import { useCallback, useEffect, type RefObject } from 'react';
import type { WorkbenchCommand } from '../components/CommandPalette';
import type { PrimaryTab } from './useUIState';
import { isEditableShortcutEvent } from './keyboardTargets';
import { useCommandPaletteShortcut } from './useCommandPaletteShortcut';
import { useWorkbenchShortcutRouter } from './useWorkbenchShortcutRouter';

interface UseOperatorKeyboardShortcutsOptions {
  activeTab: PrimaryTab;
  blockingModalOpen: boolean;
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
  setShowActivityFeed: (show: boolean) => void;
  setShowAlertsPopover: (show: boolean) => void;
  setShowKnowledgeBrowser: (show: boolean) => void;
  setShowResearchBrowser: (show: boolean) => void;
}

export function useOperatorKeyboardShortcuts({
  activeTab,
  blockingModalOpen,
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
  setShowActivityFeed,
  setShowAlertsPopover,
  setShowKnowledgeBrowser,
  setShowResearchBrowser,
}: UseOperatorKeyboardShortcutsOptions) {
  const closeTransientTargets = useCallback(() => {
    setShowResearchBrowser(false);
    setShowKnowledgeBrowser(false);
    setShowAlertsPopover(false);
    setShowActivityFeed(false);
    setResearchBrowserDirectionId(null);
    setKnowledgeBrowserDirectionId(null);
    setKnowledgeBrowserInitialDocId(null);
  }, [
    setKnowledgeBrowserDirectionId,
    setKnowledgeBrowserInitialDocId,
    setResearchBrowserDirectionId,
    setShowActivityFeed,
    setShowAlertsPopover,
    setShowKnowledgeBrowser,
    setShowResearchBrowser,
  ]);

  const closeTransientWorkbenchPanels = useCallback(() => {
    closeTransientTargets();
  }, [closeTransientTargets]);

  const openCommandPalette = useCallback(() => {
    if (commandPaletteOpen || commandPaletteShortcutBlocked) return;
    closeTransientWorkbenchPanels();
    setCommandPaletteOpen(true);
  }, [closeTransientWorkbenchPanels, commandPaletteOpen, commandPaletteShortcutBlocked, setCommandPaletteOpen]);

  useCommandPaletteShortcut(openCommandPalette, !commandPaletteOpen && !commandPaletteShortcutBlocked);

  useEffect(() => {
    function handleSearchShortcut(event: KeyboardEvent) {
      if (event.key !== '/') return;
      if (isEditableShortcutEvent(event)) return;
      if (blockingModalOpen) return;
      if (searchShortcutBlocked) return;
      event.preventDefault();
      searchInputRef.current?.focus();
    }

    document.addEventListener('keydown', handleSearchShortcut);
    return () => document.removeEventListener('keydown', handleSearchShortcut);
  }, [blockingModalOpen, searchInputRef, searchShortcutBlocked]);

  const shouldHandleWorkbenchShortcut = useCallback((commandId: string) => {
    if (blockingModalOpen) return false;
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
    enabled: !commandPaletteOpen,
    onBeforeRun: closeTransientWorkbenchPanels,
    shouldHandleShortcut: shouldHandleWorkbenchShortcut,
  });

  return { openCommandPalette };
}
