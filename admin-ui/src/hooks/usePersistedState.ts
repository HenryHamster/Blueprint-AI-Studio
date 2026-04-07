import { useState, useEffect } from 'react';

export function usePersistedState<T>(key: string, defaultValue: T): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [state, setState] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(`blueprint-admin:${key}`);
      return stored ? JSON.parse(stored) : defaultValue;
    } catch { return defaultValue; }
  });
  useEffect(() => {
    localStorage.setItem(`blueprint-admin:${key}`, JSON.stringify(state));
  }, [key, state]);
  return [state, setState];
}
