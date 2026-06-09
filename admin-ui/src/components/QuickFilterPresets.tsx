import type { DateRange } from '../hooks/useSearchFilters';
import { FAILURE_CLASSES } from '../hooks/useSearchFilters';
import { useTaskCapabilities } from '../taskCapabilities';
import { FAILURE_CLASS_CONFIG } from './TaskCard';

interface QuickFilterPresetsProps {
  statusFilter: string[];
  dateRange: DateRange;
  requiresApproval: boolean;
  blockedFilter: boolean;
  stuckFilter: boolean;
  stuckCount: number;
  stuckCount1h: number;
  stuckMinSeconds: number;
  searchQuery: string;
  failureClassFilter?: string[];
  setStatusFilter: (v: string[]) => void;
  setTaskTypeFilter: (v: string[]) => void;
  setDateRange: (v: DateRange) => void;
  setRequiresApproval: (v: boolean) => void;
  setBlockedFilter: (v: boolean) => void;
  setStuckFilter: (v: boolean) => void;
  setStuckMinSeconds: (v: number) => void;
  setSearchQuery: (v: string) => void;
  setFailureClassFilter?: (v: string[]) => void;
}

// Must stay length-aligned with STATUS_OPTIONS in ../constants — preset
// active-state detection compares statusFilter.length === ALL_STATUSES.length.
const ALL_STATUSES = ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'BLOCKED', 'PARKED', 'FAILED', 'CANCELLED', 'COMPLETED'];

export function QuickFilterPresets({
  statusFilter, dateRange, requiresApproval, blockedFilter, stuckFilter, stuckCount, stuckCount1h, stuckMinSeconds, searchQuery,
  setStatusFilter, setTaskTypeFilter, setDateRange, setRequiresApproval, setBlockedFilter, setStuckFilter, setStuckMinSeconds, setSearchQuery,
  failureClassFilter = [],
  setFailureClassFilter,
}: QuickFilterPresetsProps) {
  const { filterIds } = useTaskCapabilities();
  const isFailedToday = statusFilter.length === 1 && statusFilter[0] === 'FAILED' && dateRange === '24h' && !requiresApproval && searchQuery === '';
  const isStuckHour = stuckFilter && stuckMinSeconds === 3600 && statusFilter.length === ALL_STATUSES.length && dateRange === 'all' && searchQuery === '';
  const isNeedsReview = requiresApproval && statusFilter.length === ALL_STATUSES.length && dateRange === 'all' && searchQuery === '';
  const isBlocked = blockedFilter && statusFilter.length === ALL_STATUSES.length && dateRange === 'all' && !requiresApproval && searchQuery === '';
  const isParked = statusFilter.length === 1 && statusFilter[0] === 'PARKED' && dateRange === 'all' && !requiresApproval && !blockedFilter && !stuckFilter && searchQuery === '' && failureClassFilter.length === 0;
  const isStuck = stuckFilter && stuckMinSeconds === 0 && statusFilter.length === ALL_STATUSES.length && dateRange === 'all' && searchQuery === '';

  const clearAll = () => {
    setStatusFilter(ALL_STATUSES);
    setTaskTypeFilter([...filterIds]);
    setDateRange('all');
    setRequiresApproval(false);
    setBlockedFilter(false);
    setStuckFilter(false);
    setStuckMinSeconds(0);
    setSearchQuery('');
    setFailureClassFilter?.([]);
  };

  const applyFailureClass = (failureClass: string) => {
    if (setFailureClassFilter) {
      const isActive = failureClassFilter.length === 1 && failureClassFilter[0] === failureClass;
      setFailureClassFilter(isActive ? [] : [failureClass]);
      return;
    }
    window.dispatchEvent(new CustomEvent('blueprint:failure-class-filter', { detail: failureClass }));
  };

  const presets = [
    {
      label: 'Failed today',
      active: isFailedToday,
      danger: false,
      apply: () => {
        if (isFailedToday) { clearAll(); return; }
        setStatusFilter(['FAILED']);
        setDateRange('24h');
        setRequiresApproval(false);
        setStuckFilter(false);
        setSearchQuery('');
      },
    },
    {
      label: stuckCount1h > 0 ? `Stuck >1hr (${stuckCount1h})` : 'Stuck >1hr',
      active: isStuckHour,
      danger: true,
      apply: () => {
        if (isStuckHour) { clearAll(); return; }
        setStuckFilter(true);
        setStuckMinSeconds(3600);
        setStatusFilter(ALL_STATUSES);
        setDateRange('all');
        setRequiresApproval(false);
        setBlockedFilter(false);
        setSearchQuery('');
      },
    },
    {
      label: 'Needs review',
      active: isNeedsReview,
      danger: false,
      apply: () => {
        if (isNeedsReview) { clearAll(); return; }
        setRequiresApproval(true);
        setStatusFilter(ALL_STATUSES);
        setDateRange('all');
        setStuckFilter(false);
        setSearchQuery('');
      },
    },
    {
      label: 'Blocked',
      active: isBlocked,
      danger: false,
      apply: () => {
        if (isBlocked) { clearAll(); return; }
        setBlockedFilter(true);
        setStatusFilter(ALL_STATUSES);
        setDateRange('all');
        setRequiresApproval(false);
        setStuckFilter(false);
        setSearchQuery('');
      },
    },
    {
      label: 'Parked',
      active: isParked,
      danger: false,
      apply: () => {
        if (isParked) { clearAll(); return; }
        setStatusFilter(['PARKED']);
        setTaskTypeFilter([...filterIds]);
        setDateRange('all');
        setRequiresApproval(false);
        setBlockedFilter(false);
        setStuckFilter(false);
        setStuckMinSeconds(0);
        setSearchQuery('');
        setFailureClassFilter?.([]);
      },
    },
    {
      label: stuckCount > 0 ? `Stuck tasks (${stuckCount})` : 'Stuck tasks',
      active: isStuck,
      danger: true,
      apply: () => {
        if (isStuck) { clearAll(); return; }
        setStuckFilter(true);
        setStuckMinSeconds(0);
        setStatusFilter(ALL_STATUSES);
        setDateRange('all');
        setRequiresApproval(false);
        setBlockedFilter(false);
        setSearchQuery('');
      },
    },
  ];
  const failureClassPresets = FAILURE_CLASSES.map(failureClass => ({
    failureClass,
    label: FAILURE_CLASS_CONFIG[failureClass].label,
    active: failureClassFilter.length === 1 && failureClassFilter[0] === failureClass,
  }));

  return (
    <div className="flex flex-wrap items-center gap-1.5 mb-3">
      {presets.map(p => (
        <button key={p.label.split(' (')[0]} onClick={p.apply} className={`px-2.5 py-0.5 rounded-full text-xs border transition-colors ${
          p.active
            ? p.danger ? 'bg-red-500/20 border-red-500/40 text-red-400' : 'bg-amber-500/20 border-amber-500/40 text-amber-400'
            : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
        }`}>{p.label}</button>
      ))}
      {failureClassPresets.map(p => (
        <button
          key={p.failureClass}
          onClick={() => applyFailureClass(p.failureClass)}
          className={`px-2.5 py-0.5 rounded-full text-xs border transition-colors ${
            p.active
              ? `border-transparent ${FAILURE_CLASS_CONFIG[p.failureClass].color}`
              : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
          }`}
          title={`Filter FAILED tasks to ${p.label}`}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}
