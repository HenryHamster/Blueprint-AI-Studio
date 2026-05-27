import { usePersistedState } from './usePersistedState';
import type { UserLaunchableTaskType } from '../taskCapabilities';

export type ViewMode = 'kanban' | 'pipeline' | 'timeline' | 'home';

export function useViewPreferences() {
  const [viewMode, setViewMode] = usePersistedState<ViewMode>('viewMode', 'kanban');
  const [compactMode, setCompactMode] = usePersistedState<boolean>('compactMode', false);
  const [groupBy, setGroupBy] = usePersistedState<'status' | 'type' | 'project'>('groupBy', 'status');
  const [columnPrefs, setColumnPrefs] = usePersistedState<{ id: string; visible: boolean }[]>('kanban_column_prefs', []);
  const [defaultTaskType, setDefaultTaskType] = usePersistedState<UserLaunchableTaskType>('defaultTaskType', 'create');

  return {
    viewMode, setViewMode,
    compactMode, setCompactMode,
    groupBy, setGroupBy,
    columnPrefs, setColumnPrefs,
    defaultTaskType, setDefaultTaskType,
  };
}
