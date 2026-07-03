import { useCallback, useState } from 'react';
import type { StrategicMessageTarget } from '../strategicStateApi';

interface StrategicMessageTargetState {
  resetKey?: string | null;
  target: StrategicMessageTarget | null;
}

export function useStrategicMessageTarget(
  onOpenMessages: () => void,
  resetKey?: string | null,
) {
  const [state, setState] = useState<StrategicMessageTargetState>({ resetKey, target: null });
  const target = state.resetKey === resetKey ? state.target : null;

  const open = useCallback((next?: StrategicMessageTarget) => {
    setState({ resetKey, target: next ?? null });
    onOpenMessages();
  }, [onOpenMessages, resetKey]);

  const clear = useCallback(() => {
    setState({ resetKey, target: null });
  }, [resetKey]);

  return { target, open, clear };
}
