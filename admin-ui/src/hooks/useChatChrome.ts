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
