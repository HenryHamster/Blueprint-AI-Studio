import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useViewPreferences } from './useViewPreferences';

const STORAGE_KEY = 'blueprint-admin:viewMode';

beforeEach(() => {
  localStorage.clear();
});

describe('useViewPreferences', () => {
  it('defaults to kanban when no stored value', () => {
    const { result } = renderHook(() => useViewPreferences());
    expect(result.current.viewMode).toBe('kanban');
  });

  it('restores kanban from localStorage', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify('kanban'));
    const { result } = renderHook(() => useViewPreferences());
    expect(result.current.viewMode).toBe('kanban');
  });

  it('restores pipeline from localStorage', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify('pipeline'));
    const { result } = renderHook(() => useViewPreferences());
    expect(result.current.viewMode).toBe('pipeline');
  });

  it('migrates legacy "home" value to "kanban"', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify('home'));
    const { result } = renderHook(() => useViewPreferences());
    expect(result.current.viewMode).toBe('kanban');
  });

  it('migrates any unrecognized stored value to "kanban"', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify('unknown'));
    const { result } = renderHook(() => useViewPreferences());
    expect(result.current.viewMode).toBe('kanban');
  });
});
