import { useCallback, useEffect, useRef, useState } from 'react';
import {
  consultationIsLive,
  fetchProjectConsultations,
  type ConsultationIndexRow,
} from '../consultationDetailApi';

/**
 * Collapsed progress state for every recent Workday consultation in a project.
 *
 * One index read covers the whole transcript, so a thread with many
 * sub-sessions costs one request rather than one per row. It polls only while
 * something is actually live and stops as soon as everything is terminal —
 * a delivered consultation never changes again.
 */

const LIVE_POLL_MS = 6000;

export interface ProjectConsultationsState {
  /** Keyed by consultation id (the public reply message id). */
  rowsById: Map<string, ConsultationIndexRow>;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

const EMPTY: Map<string, ConsultationIndexRow> = new Map();

/** Rows plus the project they belong to, so another project's never show. */
interface Snapshot {
  key: string | null;
  rows: Map<string, ConsultationIndexRow>;
  error: string | null;
}

const EMPTY_SNAPSHOT: Snapshot = { key: null, rows: EMPTY, error: null };

function sameRows(
  previous: Map<string, ConsultationIndexRow>,
  next: ConsultationIndexRow[],
): boolean {
  if (previous.size !== next.length) return false;
  return next.every(row => {
    const before = previous.get(row.consultation_id);
    return (
      before !== undefined
      && before.state === row.state
      && before.outcome === row.outcome
      && before.step_count === row.step_count
      && before.delivered_at === row.delivered_at
      && before.claim_expired === row.claim_expired
    );
  });
}

export function useProjectConsultations(
  projectId: string | null,
  enabled: boolean,
): ProjectConsultationsState {
  const [snapshot, setSnapshot] = useState<Snapshot>(EMPTY_SNAPSHOT);
  const [loading, setLoading] = useState(false);
  // Read inside the interval callback so the poll cadence never has to restart
  // when the data changes; only project/enabled changes tear the loop down.
  const hasLiveRef = useRef(false);
  const [refreshToken, setRefreshToken] = useState(0);

  const refresh = useCallback(() => setRefreshToken(token => token + 1), []);

  useEffect(() => {
    if (!projectId || !enabled) {
      hasLiveRef.current = false;
      return;
    }
    let cancelled = false;
    const controller = new AbortController();

    const load = async () => {
      setLoading(true);
      try {
        const response = await fetchProjectConsultations(projectId, controller.signal);
        if (cancelled) return;
        hasLiveRef.current = response.consultations.some(row =>
          consultationIsLive(row.state));
        setSnapshot(previous => (
          // Identity is load-bearing: MessageBubble is memoized, so handing it
          // a fresh Map every poll would re-render the whole transcript.
          previous.key === projectId
            && previous.error === null
            && sameRows(previous.rows, response.consultations)
            ? previous
            : {
              key: projectId,
              rows: new Map(response.consultations.map(row => [row.consultation_id, row])),
              error: null,
            }
        ));
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : 'Failed to load consultations';
        setSnapshot(previous => ({
          key: projectId,
          rows: previous.key === projectId ? previous.rows : EMPTY,
          error: message,
        }));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    const timer = setInterval(() => {
      if (hasLiveRef.current) void load();
    }, LIVE_POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(timer);
    };
  }, [projectId, enabled, refreshToken]);

  // Derived rather than reset in the effect: a project switch must never show
  // the previous project's consultations for even one frame.
  const current = projectId && enabled && snapshot.key === projectId
    ? snapshot
    : EMPTY_SNAPSHOT;
  return { rowsById: current.rows, loading, error: current.error, refresh };
}
