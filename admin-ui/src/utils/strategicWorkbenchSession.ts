import type { PrimaryTab } from '../hooks/useUIState';
import type { StrategicInteractionMode } from '../components/StrategicSessionControl';
import type { StrategicWorkbenchState } from '../strategicStateApi';

export function normalizeStrategicInteractionMode(mode?: string | null): StrategicInteractionMode {
  if (mode === 'check_in' || mode === 'monitor' || mode === 'continuous') return mode;
  return 'chat';
}

export function workbenchSurfaceForTab(tab: PrimaryTab): StrategicWorkbenchState['active_surface'] | null {
  if (tab === 'chat') return 'strategic';
  if (tab === 'pipeline') return 'pipeline';
  if (tab === 'project') return 'project';
  if (tab === 'runtime') return 'runtime';
  return null;
}

export function tabForWorkbenchSurface(surface?: string | null): PrimaryTab | null {
  if (surface === 'strategic') return 'chat';
  if (surface === 'pipeline' || surface === 'project' || surface === 'runtime') return surface;
  return null;
}

export function buildOptimisticStrategicWorkbench(
  base: StrategicWorkbenchState | null | undefined,
  patch: Partial<StrategicWorkbenchState>,
): StrategicWorkbenchState {
  const now = new Date().toISOString();
  return {
    version: 1,
    interaction_mode: normalizeStrategicInteractionMode(
      typeof patch.interaction_mode === 'string' ? patch.interaction_mode : base?.interaction_mode,
    ),
    active_surface: typeof patch.active_surface === 'string' ? patch.active_surface : base?.active_surface ?? 'strategic',
    selected_refs: Array.isArray(patch.selected_refs) ? patch.selected_refs : base?.selected_refs ?? [],
    session_started_at: base?.session_started_at ?? now,
    last_interaction_at: now,
  };
}
