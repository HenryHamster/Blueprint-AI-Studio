import { useState } from 'react';

/**
 * Collapsed/expanded state for the strategist conversation utility chrome — the
 * "Search conversation" bar, the inline alerts banner, and the "Needs your action"
 * strip. Collapsed by default so the conversation stays clean; the operator expands
 * it on demand. Persisted per-browser under `bp.chatChromeExpanded`.
 *
 * Returns a tuple of the current expanded flag and a setter that also persists it.
 */
export function useChatChrome(): [boolean, (value: boolean) => void] {
  const [expanded, setExpanded] = useState(() => {
    try { return localStorage.getItem('bp.chatChromeExpanded') === 'true'; } catch { return false; }
  });
  const persist = (value: boolean) => {
    setExpanded(value);
    try { localStorage.setItem('bp.chatChromeExpanded', String(value)); } catch { /* ignore */ }
  };
  return [expanded, persist];
}

/**
 * Whether the operator has revealed the hidden context surfaces (alerts, cycle
 * completion, needs-action strip and filter, batch bar) while in Chat mode. Off by
 * default so Chat stays clean; the operator toggles it on to dive into detail and
 * off again to restore the clean transcript. Persisted per-browser under
 * `bp.chatDetailRevealed`.
 *
 * Returns a tuple of the current revealed flag and a setter that also persists it.
 */
export function useChatDetailRevealed(): [boolean, (value: boolean) => void] {
  const [revealed, setRevealed] = useState(() => {
    try { return localStorage.getItem('bp.chatDetailRevealed') === 'true'; } catch { return false; }
  });
  const persist = (value: boolean) => {
    setRevealed(value);
    try { localStorage.setItem('bp.chatDetailRevealed', String(value)); } catch { /* ignore */ }
  };
  return [revealed, persist];
}
