import { useMemo, useState } from 'react';
import { acceptPendingConfirmation, dismissPendingConfirmation } from '../api';
import type { PendingConfirmation, PendingConfirmationType } from '../types';
import type { StrategicStateAttentionItem } from '../strategicStateApi';
import { PendingConfirmationList } from './ProblemModelAttention';

const PENDING_CONFIRMATION_TYPES: readonly PendingConfirmationType[] = [
  'credentials_needed',
  'access_needed',
  'intent_change',
  'major_decision',
];

function attentionItemToPendingConfirmation(item: StrategicStateAttentionItem): PendingConfirmation {
  const rawType = item.confirmation_type ?? '';
  const type = (PENDING_CONFIRMATION_TYPES as readonly string[]).includes(rawType)
    ? (rawType as PendingConfirmationType)
    : 'major_decision';
  return {
    id: item.ref?.id ?? item.id,
    type,
    proposal: item.detail ?? item.label,
    created_at: item.created_at ?? undefined,
  };
}

function compactCount(value: number): string {
  return Math.abs(value) >= 1000 ? `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}k` : String(value);
}

interface PendingConfirmationsSectionProps {
  items: StrategicStateAttentionItem[];
  projectId?: string | null;
  /** Unknown permission fails closed and renders the durable rows read-only. */
  canOperateProject?: boolean;
  onResolved?: () => void;
  addToast?: (message: string, type: 'success' | 'error' | 'info') => void;
  className?: string;
  testId?: string;
}

/**
 * Shared actionable confirmation surface for both the desktop context rail and
 * the narrow-screen Context drawer. This is deliberately separate from the
 * attention summary so Chat can stay calm without making confirmations
 * unreachable.
 */
export function PendingConfirmationsSection({
  items,
  projectId,
  canOperateProject = false,
  onResolved,
  addToast,
  className = 'rounded-lg border border-neutral-800 bg-neutral-950/40 p-3',
  testId = 'pending-confirmations',
}: PendingConfirmationsSectionProps) {
  const [resolvedIds, setResolvedIds] = useState<Set<string>>(() => new Set());
  const projectByPendingId = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of items) {
      const pendingId = item.ref?.id ?? item.id;
      const itemProjectId = item.ref?.project_id ?? projectId;
      if (pendingId && itemProjectId) map.set(pendingId, itemProjectId);
    }
    return map;
  }, [items, projectId]);
  const pending = useMemo(
    () => items
      .filter(item => !resolvedIds.has(item.ref?.id ?? item.id))
      .map(attentionItemToPendingConfirmation),
    [items, resolvedIds],
  );

  if (pending.length === 0) return null;

  // Do not expose mutation controls unless permission is explicit and every
  // rendered row has a concrete project target.
  const canResolve = canOperateProject
    && pending.every(item => projectByPendingId.has(item.id));

  const handleDismiss = async (id: string, note?: string) => {
    const targetProjectId = projectByPendingId.get(id);
    if (!canResolve || !targetProjectId) return;
    try {
      await dismissPendingConfirmation(targetProjectId, id, note);
      setResolvedIds(previous => new Set(previous).add(id));
      onResolved?.();
    } catch {
      addToast?.('Failed to resolve — try again', 'error');
    }
  };

  const handleAccept = async (id: string, answer?: string) => {
    const targetProjectId = projectByPendingId.get(id);
    if (!canResolve || !targetProjectId) return;
    try {
      await acceptPendingConfirmation(targetProjectId, id, answer);
      setResolvedIds(previous => new Set(previous).add(id));
      onResolved?.();
    } catch {
      addToast?.('Failed to accept — try again', 'error');
    }
  };

  return (
    <section className={className} data-testid={testId} aria-label="Pending confirmations">
      <div className="mb-1.5 flex min-w-0 items-center justify-between gap-2">
        <h3 className="truncate text-xs font-semibold text-neutral-200">Pending confirmations</h3>
        <span className="shrink-0 font-mono text-ui-sm text-neutral-600">{compactCount(pending.length)}</span>
      </div>
      <PendingConfirmationList
        pending={pending}
        readOnly={!canResolve}
        onDismiss={canResolve ? handleDismiss : undefined}
        onAccept={canResolve ? handleAccept : undefined}
      />
    </section>
  );
}
