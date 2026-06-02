// Pure mapping from a project's lifecycle status (the backend `ProjectStatus`
// enum — DRAFT/DEPLOYING/COMPLETED/FAILED/CANCELLED, plus the legacy lowercase
// `active`/`paused` values still present on older rows) to a human-readable
// badge label and Tailwind color classes. Isolated here so it can be unit
// tested without rendering the dashboard (mirrors utils/convergenceVerdict.ts).

export interface ProjectStatusBadge {
  label: string;
  classes: string;
}

/**
 * Resolve a per-project status badge. Unknown / missing statuses fall back to a
 * neutral badge that echoes the raw value so the operator never sees a blank
 * cell — they always learn *something* about the project's true state.
 */
export function projectStatusBadge(status: string | null | undefined): ProjectStatusBadge {
  switch ((status ?? '').toUpperCase()) {
    case 'DEPLOYING':
    case 'ACTIVE': // legacy lowercase row
      return { label: 'Deploying', classes: 'bg-blue-500/15 text-blue-400 border border-blue-500/20' };
    case 'COMPLETED':
      return { label: 'Completed', classes: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/20' };
    case 'FAILED':
      return { label: 'Failed', classes: 'bg-red-500/15 text-red-400 border border-red-500/20' };
    case 'CANCELLED':
      return { label: 'Cancelled', classes: 'bg-neutral-500/15 text-neutral-400 border border-neutral-500/20' };
    case 'DRAFT':
      return { label: 'Draft', classes: 'bg-amber-500/15 text-amber-400 border border-amber-500/20' };
    case 'PAUSED': // legacy lowercase row
      return { label: 'Paused', classes: 'bg-amber-500/15 text-amber-400 border border-amber-500/20' };
    default:
      return {
        label: status ? String(status) : 'Unknown',
        classes: 'bg-neutral-500/15 text-neutral-400 border border-neutral-500/20',
      };
  }
}
