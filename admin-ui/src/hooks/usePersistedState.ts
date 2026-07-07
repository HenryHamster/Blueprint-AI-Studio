import { useState, useEffect } from 'react';

export function usePersistedState<T>(
  key: string,
  defaultValue: T,
  sanitize?: (parsed: unknown) => T,
): [T, React.Dispatch<React.SetStateAction<T>>] {
  const readValue = (): T => {
    try {
      const stored = localStorage.getItem(`blueprint-admin:${key}`);
      if (!stored) return defaultValue;
      const parsed: unknown = JSON.parse(stored);
      return sanitize ? sanitize(parsed) : (parsed as T);
    } catch { return defaultValue; }
  };
  const [state, setState] = useState<T>(readValue);
  // Re-sync from storage when the key changes (e.g. the active project switches
  // while the consuming component stays mounted). Without this, `state` keeps
  // the previous key's value AND the write effect below fires with the new key
  // + stale state, clobbering the new key's persisted value. This is the
  // sanctioned React "adjust state during render from the previous render's
  // value" idiom (tracked via state, not a ref): the re-read runs BEFORE the
  // write effect commits, so the new key's stored value is never clobbered.
  const [prevKey, setPrevKey] = useState(key);
  if (key !== prevKey) {
    setPrevKey(key);
    setState(readValue());
  }
  useEffect(() => {
    localStorage.setItem(`blueprint-admin:${key}`, JSON.stringify(state));
  }, [key, state]);
  return [state, setState];
}
