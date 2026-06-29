import { useCallback, useState } from 'react';

export function useCommandPaletteState() {
  const [open, setOpenState] = useState(false);
  const [query, setQuery] = useState('');

  const setOpen = useCallback((nextOpen: boolean) => {
    setOpenState(nextOpen);
    if (!nextOpen) setQuery('');
  }, []);

  const openWithQuery = useCallback((nextQuery: string) => {
    setQuery(nextQuery);
    setOpenState(true);
  }, []);

  return {
    open,
    query,
    setOpen,
    setQuery,
    openWithQuery,
  };
}
