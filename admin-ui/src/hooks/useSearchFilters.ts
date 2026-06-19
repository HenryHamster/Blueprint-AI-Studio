import { useEffect, useMemo, useRef } from 'react';
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

const DEFAULT_STATUS_FILTER: string[] = [...STATUS_VALUES];

// Persisted "show everything" selections from before newer statuses existed.
// Those users never chose to exclude the newly introduced status, so expand
// exact legacy-complete sets to the current full list. Deliberate partial
// selections are left untouched.
const LEGACY_COMPLETE_STATUS_VALUE_SETS: readonly string[][] = [
  ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED'],
  ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'BLOCKED', 'FAILED', 'CANCELLED', 'COMPLETED'],
];

// Keep only values present in `valid`, preserving a legitimately-empty selection.
// Falls back to `fallback` when the stored value is not an array at all.
function sanitizeStringArray(parsed: unknown, valid: readonly string[], fallback: string[]): string[] {
  if (!Array.isArray(parsed)) return fallback;
  const allowed = new Set(valid);
  return parsed.filter((v): v is string => typeof v === 'string' && allowed.has(v));
}

function sameSet(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  const setB = new Set(b);
  return a.every(v => setB.has(v));
}

// Sanitize a persisted status selection, then apply the legacy-complete → full
// expansion described on LEGACY_COMPLETE_STATUS_VALUE_SETS. The expansion is a
// no-op for any selection that isn't exactly a legacy "everything" set.
function sanitizeStatusFilter(parsed: unknown): string[] {
  const sanitized = sanitizeStringArray(parsed, STATUS_VALUES, DEFAULT_STATUS_FILTER);
  if (LEGACY_COMPLETE_STATUS_VALUE_SETS.some(legacy => sameSet(sanitized, legacy))) return [...STATUS_VALUES];
  return sanitized;
}

export function useSearchFilters(taskTypeValues: readonly string[] = FILTER_TASK_TYPE_IDS) {
  const validTaskTypes = useMemo(
    () => (taskTypeValues.length > 0 ? taskTypeValues : FILTER_TASK_TYPE_IDS),
    [taskTypeValues],
  );
  const defaultTaskTypeFilter = useMemo(() => [...validTaskTypes], [validTaskTypes]);
  const [searchQuery, setSearchQuery] = usePersistedState<string>(
    'filter:searchQuery', '', parsed => (typeof parsed === 'string' ? parsed : ''),
  );
  const [taskTypeFilter, setTaskTypeFilter] = usePersistedState<string[]>(
    'filter:taskType', defaultTaskTypeFilter,
    parsed => sanitizeStringArray(parsed, validTaskTypes, defaultTaskTypeFilter),
  );
  const [statusFilter, setStatusFilter] = usePersistedState<string[]>(
    'filter:status', DEFAULT_STATUS_FILTER, sanitizeStatusFilter,
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
  const [directionFilter, setDirectionFilter] = usePersistedState<string>(
    'filter:directionId', '',
    parsed => (typeof parsed === 'string' ? parsed : ''),
  );
  const previousValidTaskTypesRef = useRef<string[]>(defaultTaskTypeFilter);

  useEffect(() => {
    setTaskTypeFilter(prev => {
      const priorValidTaskTypes = previousValidTaskTypesRef.current;
      previousValidTaskTypesRef.current = defaultTaskTypeFilter;

      const sanitized = prev.filter(type => validTaskTypes.includes(type));
      if (sameSet(prev, priorValidTaskTypes)) return defaultTaskTypeFilter;
      if (sameSet(sanitized, prev)) return prev;
      return sanitized;
    });
  }, [defaultTaskTypeFilter, setTaskTypeFilter, validTaskTypes]);

  return { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds, failureClassFilter, setFailureClassFilter, directionFilter, setDirectionFilter };
}
