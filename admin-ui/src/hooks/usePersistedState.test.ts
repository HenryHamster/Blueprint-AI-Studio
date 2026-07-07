import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePersistedState } from './usePersistedState';

describe('usePersistedState', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('returns the default value when nothing is stored', () => {
    const { result } = renderHook(() => usePersistedState('x', 42));
    expect(result.current[0]).toBe(42);
  });

  it('round-trips a stored value', () => {
    localStorage.setItem('blueprint-admin:x', JSON.stringify(7));
    const { result } = renderHook(() => usePersistedState('x', 42));
    expect(result.current[0]).toBe(7);
  });

  it('writes state changes back to localStorage', () => {
    const { result } = renderHook(() => usePersistedState('x', 1));
    act(() => result.current[1](99));
    expect(localStorage.getItem('blueprint-admin:x')).toBe(JSON.stringify(99));
  });

  it('falls back to default on malformed JSON', () => {
    localStorage.setItem('blueprint-admin:x', '{not json');
    const { result } = renderHook(() => usePersistedState('x', 'def'));
    expect(result.current[0]).toBe('def');
  });

  it('applies the sanitize callback to the parsed value', () => {
    localStorage.setItem('blueprint-admin:x', JSON.stringify('5'));
    const { result } = renderHook(() =>
      usePersistedState<number>('x', 0, parsed => Number(parsed)),
    );
    expect(result.current[0]).toBe(5);
  });

  it('falls back to default when sanitize throws', () => {
    localStorage.setItem('blueprint-admin:x', JSON.stringify('bad'));
    const { result } = renderHook(() =>
      usePersistedState<number>('x', 0, () => { throw new Error('boom'); }),
    );
    expect(result.current[0]).toBe(0);
  });

  it('does not apply sanitize when nothing is stored', () => {
    const { result } = renderHook(() =>
      usePersistedState<number>('x', 11, () => 999),
    );
    expect(result.current[0]).toBe(11);
  });

  it('re-reads the new key value when the key changes while mounted', () => {
    localStorage.setItem('blueprint-admin:proj-a', JSON.stringify('a-val'));
    localStorage.setItem('blueprint-admin:proj-b', JSON.stringify('b-val'));
    const { result, rerender } = renderHook(
      ({ k }) => usePersistedState<string>(k, 'def'),
      { initialProps: { k: 'proj-a' } },
    );
    expect(result.current[0]).toBe('a-val');
    rerender({ k: 'proj-b' });
    // Without the key-change re-sync, this would still read 'a-val'.
    expect(result.current[0]).toBe('b-val');
  });

  it('does not clobber the new key value with the previous key state', () => {
    localStorage.setItem('blueprint-admin:proj-a', JSON.stringify('a-val'));
    localStorage.setItem('blueprint-admin:proj-b', JSON.stringify('b-val'));
    const { rerender } = renderHook(
      ({ k }) => usePersistedState<string>(k, 'def'),
      { initialProps: { k: 'proj-a' } },
    );
    rerender({ k: 'proj-b' });
    // The switch must not overwrite proj-b's persisted value with proj-a's
    // stale state (the lost-update the write effect used to cause).
    expect(localStorage.getItem('blueprint-admin:proj-b')).toBe(
      JSON.stringify('b-val'),
    );
    // proj-a's own value is left intact (no cross-project corruption).
    expect(localStorage.getItem('blueprint-admin:proj-a')).toBe(
      JSON.stringify('a-val'),
    );
  });
});
