import type { DateRange } from '../hooks/useSearchFilters';

interface QuickFilterPresetsProps {
  statusFilter: string[];
  dateRange: DateRange;
  requiresApproval: boolean;
  blockedFilter: boolean;
  stuckFilter: boolean;
  stuckCount: number;
  searchQuery: string;
  setStatusFilter: (v: string[]) => void;
  setTaskTypeFilter: (v: string[]) => void;
  setDateRange: (v: DateRange) => void;
  setRequiresApproval: (v: boolean) => void;
  setBlockedFilter: (v: boolean) => void;
  setStuckFilter: (v: boolean) => void;
  setSearchQuery: (v: string) => void;
}

const ALL_STATUSES = ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED'];
const ALL_TYPES = ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor', 'docs', 'deps', 'qa', 'security_audit', 'investigate', 'debug', 'smoketest', 'rollup', 'perf', 'strategic_review', 'design_doc_update'];

export function QuickFilterPresets({
  statusFilter, dateRange, requiresApproval, blockedFilter, stuckFilter, stuckCount, searchQuery,
  setStatusFilter, setTaskTypeFilter, setDateRange, setRequiresApproval, setBlockedFilter, setStuckFilter, setSearchQuery,
}: QuickFilterPresetsProps) {
  const isFailedToday = statusFilter.length === 1 && statusFilter[0] === 'FAILED' && dateRange === '24h' && !requiresApproval && searchQuery === '';
  const isStuckHour = statusFilter.length === 2 && statusFilter.includes('IN_PROGRESS') && statusFilter.includes('PROVISIONING') && dateRange === '1h' && !requiresApproval && searchQuery === '';
  const isNeedsReview = requiresApproval && statusFilter.length === ALL_STATUSES.length && dateRange === 'all' && searchQuery === '';
  const isBlocked = blockedFilter && statusFilter.length === ALL_STATUSES.length && dateRange === 'all' && !requiresApproval && searchQuery === '';
  const isStuck = stuckFilter && statusFilter.length === ALL_STATUSES.length && dateRange === 'all' && searchQuery === '';

  const clearAll = () => {
    setStatusFilter(ALL_STATUSES);
    setTaskTypeFilter(ALL_TYPES);
    setDateRange('all');
    setRequiresApproval(false);
    setBlockedFilter(false);
    setStuckFilter(false);
    setSearchQuery('');
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
      label: 'Active last hour',
      active: isStuckHour,
      danger: false,
      apply: () => {
        if (isStuckHour) { clearAll(); return; }
        setStatusFilter(['IN_PROGRESS', 'PROVISIONING']);
        setDateRange('1h');
        setRequiresApproval(false);
        setStuckFilter(false);
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
      label: stuckCount > 0 ? `Stuck tasks (${stuckCount})` : 'Stuck tasks',
      active: isStuck,
      danger: true,
      apply: () => {
        if (isStuck) { clearAll(); return; }
        setStuckFilter(true);
        setStatusFilter(ALL_STATUSES);
        setDateRange('all');
        setRequiresApproval(false);
        setBlockedFilter(false);
        setSearchQuery('');
      },
    },
  ];

  return (
    <div className="flex items-center gap-1.5 mb-3">
      {presets.map(p => (
        <button
          key={p.label.split(' (')[0]}
          onClick={p.apply}
          className={`px-2.5 py-0.5 rounded-full text-xs border transition-colors ${
            p.active
              ? p.danger
                ? 'bg-red-500/20 border-red-500/40 text-red-400'
                : 'bg-amber-500/20 border-amber-500/40 text-amber-400'
              : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
          }`}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}
