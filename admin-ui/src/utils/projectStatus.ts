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
    return { label: 'Completed', classes: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/20' };
  }
  if (statusUpper === 'FAILED') {
    return { label: 'Failed', classes: 'bg-red-500/15 text-red-400 border border-red-500/20' };
  }
  if (statusUpper === 'CANCELLED') {
    return { label: 'Cancelled', classes: 'bg-neutral-500/15 text-neutral-400 border border-neutral-500/20' };
  }

  // Archived: show archived state
  if (archived) {
    return { label: 'Archived', classes: 'bg-neutral-500/15 text-neutral-400 border border-neutral-500/20' };
  }

  // Paused: show paused state
  if (paused) {
    return { label: 'Paused', classes: 'bg-amber-500/15 text-amber-400 border border-amber-500/20' };
  }

  // DRAFT: show draft state
  if (statusUpper === 'DRAFT') {
    return { label: 'Draft', classes: 'bg-amber-500/15 text-amber-400 border border-amber-500/20' };
  }

  // Active projects: distinguish between bootstrapping and running
  if (statusUpper === 'DEPLOYING' || statusUpper === 'ACTIVE') {
    // Check if recently created (less than 1 hour ago)
    if (createdAt) {
      try {
        const createdMs = parseTimestamp(createdAt).getTime();
        const oneHourAgo = Date.now() - 60 * 60 * 1000;
        if (createdMs > oneHourAgo) {
          return { label: 'Bootstrapping', classes: 'bg-blue-500/15 text-blue-400 border border-blue-500/20' };
        }
      } catch {
        // If date parsing fails, fall through to "Running"
      }
    }
    return { label: 'Running', classes: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/20' };
  }

  // Legacy paused state (preserved for older rows)
  if (statusUpper === 'PAUSED') {
    return { label: 'Paused', classes: 'bg-amber-500/15 text-amber-400 border border-amber-500/20' };
  }

  // Unknown: fallback that echoes the raw value
  return {
    label: status ? String(status) : 'Unknown',
    classes: 'bg-neutral-500/15 text-neutral-400 border border-neutral-500/20',
  };
}
