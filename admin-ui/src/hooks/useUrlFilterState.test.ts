import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useUrlFilterState } from './useUrlFilterState';

describe('useUrlFilterState', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    window.history.replaceState({}, '', '/');
  });

  it('returns defaults when no URL params are present', () => {
    const { result } = renderHook(() => useUrlFilterState('srm_', 'proj-1'));
    expect(result.current.days).toBe(14);
    expect(result.current.secondaryFilter).toBe('');
    expect(result.current.taskTypeFilter).toBe('');
    expect(result.current.scopeFilter).toBe('');
    expect(result.current.page).toBe(1);
  });

  it('parses valid URL params on mount', () => {
    window.history.replaceState({}, '', '/?srm_days=30&srm_category=correctness&srm_task_type=create&srm_page=3');
    const { result } = renderHook(() => useUrlFilterState('srm_', 'proj-1'));
    expect(result.current.days).toBe(30);
    expect(result.current.secondaryFilter).toBe('correctness');
    expect(result.current.taskTypeFilter).toBe('create');
    expect(result.current.page).toBe(3);
  });

  it('falls back to default days for invalid days param value', () => {
    window.history.replaceState({}, '', '/?srm_days=999');
    const { result } = renderHook(() => useUrlFilterState('srm_', 'proj-1'));
    expect(result.current.days).toBe(14);
  });

  it('falls back to page 1 for invalid page param value', () => {
    window.history.replaceState({}, '', '/?srm_page=-5');
    const { result } = renderHook(() => useUrlFilterState('srm_', 'proj-1'));
    expect(result.current.page).toBe(1);
  });

  it('falls back to page 1 for non-numeric page param', () => {
    window.history.replaceState({}, '', '/?srm_page=abc');
    const { result } = renderHook(() => useUrlFilterState('srm_', 'proj-1'));
    expect(result.current.page).toBe(1);
  });

  it('writes state changes back to URL', () => {
    const { result } = renderHook(() => useUrlFilterState('srm_', 'proj-1'));
    act(() => { result.current.setDays(30); });
    expect(new URLSearchParams(window.location.search).get('srm_days')).toBe('30');
  });

  it('clears default values from URL (does not pollute URL with defaults)', () => {
    const { result } = renderHook(() => useUrlFilterState('srm_', 'proj-1'));
    act(() => { result.current.setDays(30); });
    act(() => { result.current.setDays(14); }); // back to default
    expect(new URLSearchParams(window.location.search).get('srm_days')).toBeNull();
  });

  it('clears filter state when projectId changes', () => {
    window.history.replaceState({}, '', '/?srm_days=30&srm_category=correctness&srm_page=3');
    const { result, rerender } = renderHook(
      ({ pid }: { pid: string }) => useUrlFilterState('srm_', pid),
      { initialProps: { pid: 'proj-1' } },
    );
    expect(result.current.days).toBe(30);

    act(() => { rerender({ pid: 'proj-2' }); });

    expect(result.current.days).toBe(14);
    expect(result.current.secondaryFilter).toBe('');
    expect(result.current.page).toBe(1);
  });

  it('does not clobber params belonging to a different prefix', () => {
    window.history.replaceState({}, '', '/?sru_days=7');
    const { result } = renderHook(() => useUrlFilterState('srm_', 'proj-1'));
    act(() => { result.current.setDays(30); });
    const params = new URLSearchParams(window.location.search);
    expect(params.get('srm_days')).toBe('30');
    expect(params.get('sru_days')).toBe('7');
  });

  it('honors a custom secondaryParam name (e.g. blind_spot for unhandled-edges)', () => {
    window.history.replaceState({}, '', '/?sru_blind_spot=race-condition');
    const { result } = renderHook(() =>
      useUrlFilterState('sru_', 'proj-1', { secondaryParam: 'blind_spot' }),
    );
    expect(result.current.secondaryFilter).toBe('race-condition');

    act(() => { result.current.setSecondaryFilter('null-deref'); });
    expect(new URLSearchParams(window.location.search).get('sru_blind_spot')).toBe('null-deref');
  });

  it('tracks an optional tertiaryParam for additional drill-through filters', () => {
    window.history.replaceState({}, '', '/?sru_case_pattern=retry-loop');
    const { result } = renderHook(() =>
      useUrlFilterState('sru_', 'proj-1', {
        secondaryParam: 'blind_spot',
        tertiaryParam: 'case_pattern',
      }),
    );
    expect(result.current.tertiaryFilter).toBe('retry-loop');

    act(() => { result.current.setTertiaryFilter('race-condition'); });
    expect(new URLSearchParams(window.location.search).get('sru_case_pattern')).toBe('race-condition');

    act(() => { result.current.setTertiaryFilter(''); });
    expect(new URLSearchParams(window.location.search).get('sru_case_pattern')).toBeNull();
  });

  it('tracks scope filter when withScope is enabled', () => {
    window.history.replaceState({}, '', '/?srm_scope=large');
    const { result } = renderHook(() =>
      useUrlFilterState('srm_', 'proj-1', { withScope: true }),
    );
    expect(result.current.scopeFilter).toBe('large');

    act(() => { result.current.setScopeFilter('small'); });
    expect(new URLSearchParams(window.location.search).get('srm_scope')).toBe('small');

    act(() => { result.current.setScopeFilter(''); });
    expect(new URLSearchParams(window.location.search).get('srm_scope')).toBeNull();
  });

  it('ignores scope param when withScope is not enabled', () => {
    window.history.replaceState({}, '', '/?sru_scope=large');
    const { result } = renderHook(() => useUrlFilterState('sru_', 'proj-1'));
    expect(result.current.scopeFilter).toBe('');
  });

  it('clears tertiary filter state when projectId changes', () => {
    window.history.replaceState({}, '', '/?sru_days=30&sru_case_pattern=retry&sru_page=3');
    const { result, rerender } = renderHook(
      ({ pid }: { pid: string }) => useUrlFilterState('sru_', pid, { tertiaryParam: 'case_pattern' }),
      { initialProps: { pid: 'proj-1' } },
    );
    expect(result.current.tertiaryFilter).toBe('retry');

    act(() => { rerender({ pid: 'proj-2' }); });

    expect(result.current.days).toBe(14);
    expect(result.current.tertiaryFilter).toBe('');
    expect(result.current.page).toBe(1);
  });
});
