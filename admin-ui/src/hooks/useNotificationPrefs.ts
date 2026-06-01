import { useEffect, useRef } from 'react';
import { usePersistedState } from './usePersistedState';

/**
 * Persisted desktop-notification / sound preferences plus mirror refs.
 *
 * The refs hold the latest value of each toggle so async fetch callbacks
 * (e.g. the task-polling loop in useTasksState) can read the current
 * preference without re-subscribing on every change. Returning the refs
 * keeps the existing facade behavior and timing identical.
 */
export function useNotificationPrefs() {
  const [notifyOnComplete, setNotifyOnComplete] = usePersistedState<boolean>('notifyOnComplete', false);
  const [notifyOnFail, setNotifyOnFail] = usePersistedState<boolean>('notifyOnFail', false);
  const [notifyOnApprovalNeeded, setNotifyOnApprovalNeeded] = usePersistedState<boolean>('notifyOnApprovalNeeded', false);
  const [soundOnComplete, setSoundOnComplete] = usePersistedState<boolean>('soundOnComplete', false);
  const [notifyOnHighSignalMessage, setNotifyOnHighSignalMessage] = usePersistedState<boolean>('notifyOnHighSignalMessage', false);

  const notifyRef = useRef(notifyOnComplete);
  useEffect(() => { notifyRef.current = notifyOnComplete; }, [notifyOnComplete]);
  const notifyOnFailRef = useRef(notifyOnFail);
  useEffect(() => { notifyOnFailRef.current = notifyOnFail; }, [notifyOnFail]);
  const notifyOnApprovalNeededRef = useRef(notifyOnApprovalNeeded);
  useEffect(() => { notifyOnApprovalNeededRef.current = notifyOnApprovalNeeded; }, [notifyOnApprovalNeeded]);
  const soundOnCompleteRef = useRef(soundOnComplete);
  useEffect(() => { soundOnCompleteRef.current = soundOnComplete; }, [soundOnComplete]);
  const notifyOnHighSignalMessageRef = useRef(notifyOnHighSignalMessage);
  useEffect(() => { notifyOnHighSignalMessageRef.current = notifyOnHighSignalMessage; }, [notifyOnHighSignalMessage]);

  return {
    notifyOnComplete, setNotifyOnComplete,
    notifyOnFail, setNotifyOnFail,
    notifyOnApprovalNeeded, setNotifyOnApprovalNeeded,
    soundOnComplete, setSoundOnComplete,
    notifyOnHighSignalMessage, setNotifyOnHighSignalMessage,
    // Refs mirroring the latest toggle values for async-callback reads.
    notifyRef, notifyOnFailRef, notifyOnApprovalNeededRef,
    soundOnCompleteRef, notifyOnHighSignalMessageRef,
  };
}
