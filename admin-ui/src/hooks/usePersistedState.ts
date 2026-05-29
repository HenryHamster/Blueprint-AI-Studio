import { useState, useEffect } from 'react';

export function usePersistedState<T>(
  key: string,
  defaultValue: T,
  sanitize?: (parsed: unknown) => T,
): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [state, setState] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(`blueprint-admin:${key}`);
      if (!stored) return defaultValue;
      const parsed: unknown = JSON.parse(stored);
      return sanitize ? sanitize(parsed) : (parsed as T);
    } catch { return defaultValue; }
  });
  useEffect(() => {
    localStorage.setItem(`blueprint-admin:${key}`, JSON.stringify(state));
  }, [key, state]);
  return [state, setState];
}
