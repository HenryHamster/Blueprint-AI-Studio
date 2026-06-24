// Pure mapping from a project's lifecycle status (the backend `ProjectStatus`
// enum — DRAFT/DEPLOYING/COMPLETED/FAILED/CANCELLED, plus the legacy lowercase
// `active`/`paused` values still present on older rows) plus contextual signals
// (paused flag, archived flag, creation time) to a human-readable badge label
// and Tailwind color classes. Isolated here so it can be unit tested without
// rendering the dashboard (mirrors utils/convergenceVerdict.ts).

import { parseTimestamp } from './time';

export interface ProjectStatusBadge {
  label: string;
  classes: string;
  /** True when the badge reflects stale/last-known data rather than a live
   *  reading (the dashboard's last project fetch failed). Renderers use this
   *  to show a degraded affordance; the label already carries a "· stale"
   *  suffix and the classes are desaturated/dimmed. */
  stale: boolean;
}

export interface ProjectStatusInput {
  status?: string | null;
  paused?: boolean;
  archived?: boolean;
  created_at?: string;
  /** When true, the underlying data is stale (e.g. the last backend refresh
   *  failed), so the badge must visibly degrade instead of presenting a
   *  last-known reading as authoritative. Display-only. */
  stale?: boolean;
}

// Tailwind classes appended to any badge whose data is stale: desaturate +
// dim so a last-known reading never reads as a live, authoritative one.
const STALE_CLASSES = 'opacity-60 saturate-50';

/** Semantic tone for status/verdict badges — maps to @theme status tokens. */
export type StatusTone = 'success' | 'blocking' | 'warning' | 'info' | 'neutral';

const STATUS_TONE_CLASSES: Record<StatusTone, string> = {
  success:  'bg-status-success/15 text-status-success border border-status-success/20',
  blocking: 'bg-status-blocking/15 text-status-blocking border border-status-blocking/20',
  warning:  'bg-status-warning/15 text-status-warning border border-status-warning/20',
  info:     'bg-status-info/15 text-status-info border border-status-info/20',
  neutral:  'bg-status-neutral/15 text-status-neutral border border-status-neutral/20',
};

/** Canonical token-backed badge class string for a given semantic tone. */
export function statusBadgeClasses(tone: StatusTone): string {
  return STATUS_TONE_CLASSES[tone];
}

/**
 * Determine if a project is actively running (not paused, not archived, with a
 * DEPLOYING/ACTIVE status). Used to count "active" projects in the dashboard summary.
 * Reuses the canonical badge logic so the count and badge can never drift apart.
 */
export function isProjectActive(input: ProjectStatusInput | null | undefined): boolean {
  if (!input || typeof input === 'string') return false;

  const status = (input.status ?? '').toUpperCase();
  const paused = input.paused ?? false;
  const archived = input.archived ?? false;

  // A project is active if it's deploying/active, not paused, and not archived
  const isDeploying = status === 'DEPLOYING' || status === 'ACTIVE';
  return isDeploying && !paused && !archived;
}

/**
 * Resolve a per-project status badge that reflects its real operating state.
 * Accepts either a project-like object with status/paused/archived/created_at
 * or a plain status string (for backward compatibility).
 *
 * Returns a truthful badge based on:
 * - Terminal states (COMPLETED/FAILED/CANCELLED): show the terminal state
 * - Archived: show "Archived"
 * - Paused: show "Paused"
 * - DRAFT: show "Draft"
 * - Recently created (<1 hour): show "Bootstrapping"
 * - Active DEPLOYING: show "Running"
 * - Unknown/missing: fallback neutral badge
 */
export function projectStatusBadge(
  input: ProjectStatusInput | string | null | undefined
): ProjectStatusBadge {
  const stale =
    typeof input === 'string' || input === null || input === undefined
      ? false
      : input.stale ?? false;
  const base = resolveBaseBadge(input);
  if (!stale) return { ...base, stale: false };
  // Degrade: desaturate/dim and mark the label so a last-known reading is never
  // presented as a live, authoritative one. Display-only — the data is unchanged.
  return {
    label: `${base.label} · stale`,
    classes: `${base.classes} ${STALE_CLASSES}`,
    stale: true,
  };
}

/** Resolve the truthful badge for live data, ignoring freshness. */
function resolveBaseBadge(
  input: ProjectStatusInput | string | null | undefined
): { label: string; classes: string } {
  // Handle backward compatibility: if input is a string, treat it as status
  let status: string | null | undefined;
  let paused = false;
  let archived = false;
  let createdAt: string | null | undefined;

  if (typeof input === 'string' || input === null || input === undefined) {
    status = input;
  } else {
    status = input.status;
    paused = input.paused ?? false;
    archived = input.archived ?? false;
    createdAt = input.created_at;
  }

  const statusUpper = (status ?? '').toUpperCase();

  // Terminal states: show them regardless of other signals
  if (statusUpper === 'COMPLETED') {
    return { label: 'Completed', classes: statusBadgeClasses('success') };
  }
  if (statusUpper === 'FAILED') {
    return { label: 'Failed', classes: statusBadgeClasses('blocking') };
  }
  if (statusUpper === 'CANCELLED') {
    return { label: 'Cancelled', classes: statusBadgeClasses('neutral') };
  }

  // Archived: show archived state
  if (archived) {
    return { label: 'Archived', classes: statusBadgeClasses('neutral') };
  }

  // Paused: show paused state
  if (paused) {
    return { label: 'Paused', classes: statusBadgeClasses('warning') };
  }

  // DRAFT: show draft state
  if (statusUpper === 'DRAFT') {
    return { label: 'Draft', classes: statusBadgeClasses('warning') };
  }

  // Active projects: distinguish between bootstrapping and running
  if (statusUpper === 'DEPLOYING' || statusUpper === 'ACTIVE') {
    // Check if recently created (less than 1 hour ago)
    if (createdAt) {
      try {
        const createdMs = parseTimestamp(createdAt).getTime();
        const oneHourAgo = Date.now() - 60 * 60 * 1000;
        if (createdMs > oneHourAgo) {
          return { label: 'Bootstrapping', classes: statusBadgeClasses('info') };
        }
      } catch {
        // If date parsing fails, fall through to "Running"
      }
    }
    return { label: 'Running', classes: statusBadgeClasses('success') };
  }

  // Legacy paused state (preserved for older rows)
  if (statusUpper === 'PAUSED') {
    return { label: 'Paused', classes: statusBadgeClasses('warning') };
  }

  // Unknown: fallback that echoes the raw value
  return {
    label: status ? String(status) : 'Unknown',
    classes: statusBadgeClasses('neutral'),
  };
}
