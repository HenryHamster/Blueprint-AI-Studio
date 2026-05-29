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
});
