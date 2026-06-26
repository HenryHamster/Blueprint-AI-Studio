import { usePersistedState } from './usePersistedState';
import type { UserLaunchableTaskType } from '../taskCapabilities';

export type ViewMode = 'kanban' | 'pipeline';

export function useViewPreferences() {
  const [viewMode, setViewMode] = usePersistedState<ViewMode>('viewMode', 'kanban', (v) => {
    // Migrate legacy 'home' value stored before viewMode was decoupled from PrimaryTab
    if (v === 'kanban' || v === 'pipeline') return v;
    return 'kanban';
  });
  const [compactMode, setCompactMode] = usePersistedState<boolean>('compactMode', false);
  const [columnPrefs, setColumnPrefs] = usePersistedState<{ id: string; visible: boolean }[]>('kanban_column_prefs', []);
  const [defaultTaskType, setDefaultTaskType] = usePersistedState<UserLaunchableTaskType>('defaultTaskType', 'create');

  return {
    viewMode, setViewMode,
    compactMode, setCompactMode,
    columnPrefs, setColumnPrefs,
    defaultTaskType, setDefaultTaskType,
  };
}
