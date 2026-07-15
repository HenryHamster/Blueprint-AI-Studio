import { getIdToken } from './firebase';

export type UiLoadTimingSampleType = 'first_commit' | 'chat_data_ready' | 'board_data_ready';

export function currentUiSurface(): string {
  return (window.location.hash.replace(/^#\/?/, '').split('/')[0] || 'chat').slice(0, 64);
}

/** Best-effort telemetry: load timing must never delay or fail the UI. */
export function reportUiLoadTiming(
  surface: string,
  pageReadyMs: number,
  sampleType: UiLoadTimingSampleType,
): void {
  getIdToken().then(token => {
    if (!token) return;
    return fetch('/api/admin/ui-load-timing', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ surface: surface.slice(0, 64), page_ready_ms: pageReadyMs, sample_type: sampleType }),
    }).catch(() => {});
  }).catch(() => {});
}
