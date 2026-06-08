import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useSearchFilters, FAILURE_CLASSES } from './useSearchFilters';
import { FAILURE_CLASS_CONFIG } from '../components/TaskCard';
import { FILTER_TASK_TYPE_IDS } from '../taskCapabilities';
import { STATUS_OPTIONS } from '../constants';

const STATUS_VALUES = STATUS_OPTIONS.map(o => o.value);
const k = (name: string) => `blueprint-admin:filter:${name}`;

describe('useSearchFilters', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('returns defaults when storage is empty', () => {
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.searchQuery).toBe('');
    expect(result.current.taskTypeFilter).toEqual([...FILTER_TASK_TYPE_IDS]);
    expect(result.current.statusFilter).toEqual(STATUS_VALUES);
    expect(result.current.dateRange).toBe('all');
    expect(result.current.blockedFilter).toBe(false);
    expect(result.current.requiresApproval).toBe(false);
    expect(result.current.stuckFilter).toBe(false);
    expect(result.current.stuckMinSeconds).toBe(0);
    expect(result.current.failureClassFilter).toEqual([]);
  });

  it('persists changes to the filter: keys', () => {
    const { result } = renderHook(() => useSearchFilters());
    act(() => result.current.setSearchQuery('hello'));
    act(() => result.current.setStuckMinSeconds(120));
    expect(localStorage.getItem(k('searchQuery'))).toBe(JSON.stringify('hello'));
    expect(localStorage.getItem(k('stuckMinSeconds'))).toBe(JSON.stringify(120));
  });

  it('round-trips a stored selection', () => {
    localStorage.setItem(k('status'), JSON.stringify(['FAILED', 'COMPLETED']));
    localStorage.setItem(k('dateRange'), JSON.stringify('6h'));
    localStorage.setItem(k('blocked'), JSON.stringify(true));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual(['FAILED', 'COMPLETED']);
    expect(result.current.dateRange).toBe('6h');
    expect(result.current.blockedFilter).toBe(true);
  });

  it('preserves a legitimately-empty status selection (does not snap to default)', () => {
    localStorage.setItem(k('status'), JSON.stringify([]));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual([]);
  });

  it('preserves a legitimately-empty task-type selection', () => {
    localStorage.setItem(k('taskType'), JSON.stringify([]));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.taskTypeFilter).toEqual([]);
  });

  it('drops unknown statuses, task types, and failure classes on restore', () => {
    localStorage.setItem(k('status'), JSON.stringify(['FAILED', 'BOGUS']));
    localStorage.setItem(k('taskType'), JSON.stringify(['create', 'not-a-type']));
    localStorage.setItem(k('failureClass'), JSON.stringify(['CODE_ERROR', 'NOPE']));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual(['FAILED']);
    expect(result.current.taskTypeFilter).toEqual(['create']);
    expect(result.current.failureClassFilter).toEqual(['CODE_ERROR']);
  });

  it('expands a legacy all-statuses selection (pre-BLOCKED) to the full current list', () => {
    // A user who had "everything" selected before BLOCKED existed persisted the 7
    // legacy statuses. Without migration that now reads as a deliberate 7-of-8
    // subset that hides BLOCKED (and triggers a server-side &status= filter). The
    // sanitizer must restore it to the full current set so "everything" stays so.
    const legacy = ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED'];
    localStorage.setItem(k('status'), JSON.stringify(legacy));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual(STATUS_VALUES);
    expect(result.current.statusFilter).toContain('BLOCKED');
    expect(result.current.statusFilter).toContain('PARKED');
  });

  it('expands a legacy all-statuses selection (pre-PARKED) to the full current list', () => {
    const legacy = ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'BLOCKED', 'FAILED', 'CANCELLED', 'COMPLETED'];
    localStorage.setItem(k('status'), JSON.stringify(legacy));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual(STATUS_VALUES);
    expect(result.current.statusFilter).toContain('PARKED');
  });

  it('preserves a deliberate partial status selection that omits BLOCKED and PARKED', () => {
    // A strict subset of the legacy set is a real user choice, not a "show
    // everything" snapshot — leave it untouched even though it predates BLOCKED.
    const partial = ['QUEUED', 'IN_PROGRESS', 'EVALUATING'];
    localStorage.setItem(k('status'), JSON.stringify(partial));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual(partial);
    expect(result.current.statusFilter).not.toContain('BLOCKED');
    expect(result.current.statusFilter).not.toContain('PARKED');
  });

  it('leaves a partial selection that already includes PARKED untouched', () => {
    // Once PARKED is part of the stored selection, the expansion must be a no-op
    // so it does not fight a user who deliberately deselected other statuses.
    const withParked = ['PARKED', 'FAILED'];
    localStorage.setItem(k('status'), JSON.stringify(withParked));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual(withParked);
  });

  it('falls back to default when a stored array value is not an array', () => {
    localStorage.setItem(k('status'), JSON.stringify('FAILED'));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual(STATUS_VALUES);
  });

  it('coerces non-boolean stored values to booleans', () => {
    localStorage.setItem(k('blocked'), JSON.stringify('yes'));
    localStorage.setItem(k('stuck'), JSON.stringify(1));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.blockedFilter).toBe(false);
    expect(result.current.stuckFilter).toBe(false);
  });

  it('coerces negative and NaN stuckMinSeconds to 0', () => {
    localStorage.setItem(k('stuckMinSeconds'), JSON.stringify(-5));
    const { result: r1 } = renderHook(() => useSearchFilters());
    expect(r1.current.stuckMinSeconds).toBe(0);

    localStorage.setItem(k('stuckMinSeconds'), JSON.stringify('abc'));
    const { result: r2 } = renderHook(() => useSearchFilters());
    expect(r2.current.stuckMinSeconds).toBe(0);
  });

  it('falls back to all for an unknown dateRange', () => {
    localStorage.setItem(k('dateRange'), JSON.stringify('99y'));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.dateRange).toBe('all');
  });

  it('coerces a non-string searchQuery to empty string', () => {
    localStorage.setItem(k('searchQuery'), JSON.stringify(123));
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.searchQuery).toBe('');
  });

  it('falls back to defaults on malformed JSON', () => {
    localStorage.setItem(k('status'), '{bad json');
    const { result } = renderHook(() => useSearchFilters());
    expect(result.current.statusFilter).toEqual(STATUS_VALUES);
  });

  it('exposes the full failure-class list', () => {
    expect(FAILURE_CLASSES.length).toBe(Object.keys(FAILURE_CLASS_CONFIG).length);
    expect(FAILURE_CLASSES.length).toBeGreaterThan(0);
  });

  it('uses the resolved contract-driven task types for defaults', () => {
    const { result } = renderHook(() => useSearchFilters(['create', 'release_audit']));
    expect(result.current.taskTypeFilter).toEqual(['create', 'release_audit']);
  });

  it('expands an all-types selection when the published filter-visible set grows', () => {
    const { result, rerender } = renderHook(
      ({ taskTypes }) => useSearchFilters(taskTypes),
      { initialProps: { taskTypes: ['create', 'fix'] as string[] } },
    );

    expect(result.current.taskTypeFilter).toEqual(['create', 'fix']);

    rerender({ taskTypes: ['create', 'fix', 'release_audit'] });

    expect(result.current.taskTypeFilter).toEqual(['create', 'fix', 'release_audit']);
  });

  it('preserves an explicit subset when the published filter-visible set grows', () => {
    const { result, rerender } = renderHook(
      ({ taskTypes }) => useSearchFilters(taskTypes),
      { initialProps: { taskTypes: ['create', 'fix'] as string[] } },
    );

    act(() => result.current.setTaskTypeFilter(['create']));
    rerender({ taskTypes: ['create', 'fix', 'release_audit'] });

    expect(result.current.taskTypeFilter).toEqual(['create']);
  });
});
