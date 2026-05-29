import { usePersistedState } from './usePersistedState';
import { FILTER_TASK_TYPE_IDS } from '../taskCapabilities';
import { STATUS_OPTIONS } from '../constants';
import { FAILURE_CLASS_CONFIG } from '../components/TaskCard';

export type DateRange = 'all' | '1h' | '6h' | '24h' | '7d';
// Failure-class keys are runtime strings persisted by the backend classifier
// (note one is the lowercase literal "agent_stalled"); the canonical set is the
// keys of FAILURE_CLASS_CONFIG.
export type FailureClass = string;

// Single source of truth: derive the selectable failure classes from the shared
// FAILURE_CLASS_CONFIG map so adding a class to that map flows everywhere
// (filter dropdown, quick-filter chips) without a second hand-maintained list.
export const FAILURE_CLASSES: FailureClass[] = Object.keys(FAILURE_CLASS_CONFIG);

const DATE_RANGES: DateRange[] = ['all', '1h', '6h', '24h', '7d'];
const STATUS_VALUES = STATUS_OPTIONS.map(o => o.value);
const TASK_TYPE_VALUES = FILTER_TASK_TYPE_IDS as readonly string[];

const DEFAULT_STATUS_FILTER: string[] = [...STATUS_VALUES];
const DEFAULT_TASK_TYPE_FILTER: string[] = [...FILTER_TASK_TYPE_IDS];

// Keep only values present in `valid`, preserving a legitimately-empty selection.
// Falls back to `fallback` when the stored value is not an array at all.
function sanitizeStringArray(parsed: unknown, valid: readonly string[], fallback: string[]): string[] {
  if (!Array.isArray(parsed)) return fallback;
  const allowed = new Set(valid);
  return parsed.filter((v): v is string => typeof v === 'string' && allowed.has(v));
}

export function useSearchFilters() {
  const [searchQuery, setSearchQuery] = usePersistedState<string>(
    'filter:searchQuery', '', parsed => (typeof parsed === 'string' ? parsed : ''),
  );
  const [taskTypeFilter, setTaskTypeFilter] = usePersistedState<string[]>(
    'filter:taskType', DEFAULT_TASK_TYPE_FILTER,
    parsed => sanitizeStringArray(parsed, TASK_TYPE_VALUES, DEFAULT_TASK_TYPE_FILTER),
  );
  const [statusFilter, setStatusFilter] = usePersistedState<string[]>(
    'filter:status', DEFAULT_STATUS_FILTER,
    parsed => sanitizeStringArray(parsed, STATUS_VALUES, DEFAULT_STATUS_FILTER),
  );
  const [dateRange, setDateRange] = usePersistedState<DateRange>(
    'filter:dateRange', 'all',
    parsed => (DATE_RANGES.includes(parsed as DateRange) ? (parsed as DateRange) : 'all'),
  );
  const [blockedFilter, setBlockedFilter] = usePersistedState<boolean>(
    'filter:blocked', false, parsed => parsed === true,
  );
  const [requiresApproval, setRequiresApproval] = usePersistedState<boolean>(
    'filter:requiresApproval', false, parsed => parsed === true,
  );
  const [stuckFilter, setStuckFilter] = usePersistedState<boolean>(
    'filter:stuck', false, parsed => parsed === true,
  );
  const [stuckMinSeconds, setStuckMinSeconds] = usePersistedState<number>(
    'filter:stuckMinSeconds', 0,
    parsed => {
      const n = Number(parsed);
      return Number.isFinite(n) && n >= 0 ? n : 0;
    },
  );
  const [failureClassFilter, setFailureClassFilter] = usePersistedState<string[]>(
    'filter:failureClass', [],
    parsed => sanitizeStringArray(parsed, FAILURE_CLASSES, []),
  );

  return { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds, failureClassFilter, setFailureClassFilter };
}
