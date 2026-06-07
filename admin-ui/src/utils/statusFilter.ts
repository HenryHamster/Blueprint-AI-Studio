import { STATUS_OPTIONS } from '../constants';

// Canonical set of statuses the status-filter UI can select. A task whose status
// is NOT in this set is "unknown" to the frontend (a status the backend added but
// this UI build doesn't yet know about).
const KNOWN_STATUSES: readonly string[] = STATUS_OPTIONS.map(o => o.value);

/**
 * Whether a task's status passes the active status filter.
 *
 * This mirrors `matchesTaskTypeFilter`'s unknown-value escape hatch so the filter
 * fails *open* for statuses this UI build predates:
 *   - If the status is explicitly selected, it matches.
 *   - Otherwise, if the filter is anything short of "all known statuses selected"
 *     (i.e. the user is actively narrowing the view), an unknown status is hidden
 *     along with everything else that wasn't picked.
 *   - But when the status filter is full (no active filtering), an unknown status
 *     shows instead of silently vanishing.
 *
 * Without this, the next backend-added TaskStatus would be invisible in every
 * list/board view until the constant is updated — exactly the BLOCKED regression
 * this fix addresses, generalized so it doesn't recur.
 *
 * @param status        the task's status string
 * @param selected      the currently selected status filter values
 * @param known         the full set of statuses the UI can select (defaults to STATUS_OPTIONS)
 */
export function matchesStatusFilter(
  status: string,
  selected: readonly string[],
  known: readonly string[] = KNOWN_STATUSES,
): boolean {
  if (selected.includes(status)) return true;
  // Active filtering (a strict subset selected) excludes everything unpicked,
  // unknown statuses included.
  if (selected.length !== known.length) return false;
  // Filter is full → no active filtering → fail open for unknown statuses.
  return !known.includes(status);
}
