import { usePersistedState } from './usePersistedState';

export function useViewPreferences() {
  const [viewMode, setViewMode] = usePersistedState<'kanban' | 'pipeline' | 'home'>('viewMode', 'kanban');
  const [compactMode, setCompactMode] = usePersistedState<boolean>('compactMode', false);
  const [groupBy, setGroupBy] = usePersistedState<'status' | 'type' | 'project'>('groupBy', 'status');
  const [columnPrefs, setColumnPrefs] = usePersistedState<{ id: string; visible: boolean }[]>('kanban_column_prefs', []);
  const [defaultTaskType, setDefaultTaskType] = usePersistedState<'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor'>('defaultTaskType', 'create');

  return {
    viewMode, setViewMode,
    compactMode, setCompactMode,
    groupBy, setGroupBy,
    columnPrefs, setColumnPrefs,
    defaultTaskType, setDefaultTaskType,
  };
}
