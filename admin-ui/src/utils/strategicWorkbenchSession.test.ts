import { describe, expect, test } from 'vitest';
import {
  MAX_STRATEGIC_SELECTED_REFS,
  buildOptimisticStrategicWorkbench,
  normalizeStrategicSelectedRefs,
  tabForWorkbenchSurface,
  removeStrategicSelectedRef,
  upsertStrategicSelectedRef,
  workbenchSurfaceForTab,
} from './strategicWorkbenchSession';

describe('strategicWorkbenchSession selected refs', () => {
  test('maps only canonical workbench tabs to persisted surfaces', () => {
    expect(workbenchSurfaceForTab('chat')).toBe('strategic');
    expect(workbenchSurfaceForTab('pipeline')).toBe('pipeline');
    expect(workbenchSurfaceForTab('project')).toBe('project');
    expect(workbenchSurfaceForTab('runtime')).toBe('runtime');
    expect(tabForWorkbenchSurface('strategic')).toBe('chat');
    expect(tabForWorkbenchSurface('pipeline')).toBe('pipeline');
    expect(tabForWorkbenchSurface('project')).toBe('project');
    expect(tabForWorkbenchSurface('runtime')).toBe('runtime');
    expect(tabForWorkbenchSurface('unknown')).toBeNull();
  });

  test('normalizes selected refs with dedupe and cap', () => {
    const refs = normalizeStrategicSelectedRefs([
      { kind: ' task ', id: 'abc', label: '  Active   task  ' },
      { kind: 'task', id: 'abc', label: 'Duplicate' },
      { kind: '', id: 'missing-kind' },
      { kind: 'direction', id: 'd-1' },
      ...Array.from({ length: 20 }, (_, i) => ({ kind: 'node', id: `n-${i}` })),
    ]);

    expect(refs).toHaveLength(MAX_STRATEGIC_SELECTED_REFS);
    expect(refs[0]).toEqual({ kind: 'task', id: 'abc', label: 'Active task' });
    expect(refs[1]).toEqual({ kind: 'direction', id: 'd-1' });
    expect(refs.filter(ref => ref.kind === 'task' && ref.id === 'abc')).toHaveLength(1);
  });

  test('upserts refs at the front and removes by kind/id', () => {
    const base = [
      { kind: 'task', id: 'old', label: 'Old task' },
      { kind: 'direction', id: 'd-1', label: 'Direction' },
    ];

    const upserted = upsertStrategicSelectedRef(base, { kind: 'task', id: 'old', label: 'Updated task' });
    expect(upserted[0]).toEqual({ kind: 'task', id: 'old', label: 'Updated task' });
    expect(upserted).toHaveLength(2);

    expect(removeStrategicSelectedRef(upserted, { kind: 'task', id: 'old' }))
      .toEqual([{ kind: 'direction', id: 'd-1', label: 'Direction' }]);
  });

  test('optimistic workbench normalizes patched refs', () => {
    const optimistic = buildOptimisticStrategicWorkbench(null, {
      selected_refs: [
        { kind: 'task', id: 't-1', label: 'Task 1' },
        { kind: 'task', id: 't-1', label: 'Task 1 duplicate' },
      ],
    });

    expect(optimistic.selected_refs).toEqual([{ kind: 'task', id: 't-1', label: 'Task 1' }]);
    expect(optimistic.session_started_at).toBeTruthy();
    expect(optimistic.last_interaction_at).toBeTruthy();
  });
});
