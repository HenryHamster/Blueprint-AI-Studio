import { useState } from 'react';

export type DateRange = 'all' | '1h' | '6h' | '24h' | '7d';

export function useSearchFilters() {
  const [searchQuery, setSearchQuery] = useState('');
  const [taskTypeFilter, setTaskTypeFilter] = useState<string[]>([
    'create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor',
    'docs', 'deps', 'qa', 'security_audit', 'investigate', 'debug', 'smoketest', 'rollup',
    'perf', 'strategic_review', 'design_doc_update',
  ]);
  const [statusFilter, setStatusFilter] = useState<string[]>([
    'QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED',
  ]);
  const [dateRange, setDateRange] = useState<DateRange>('all');
  const [blockedFilter, setBlockedFilter] = useState(false);
  const [requiresApproval, setRequiresApproval] = useState(false);
  const [stuckFilter, setStuckFilter] = useState(false);
  const [stuckMinSeconds, setStuckMinSeconds] = useState(0);

  return { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange, blockedFilter, setBlockedFilter, requiresApproval, setRequiresApproval, stuckFilter, setStuckFilter, stuckMinSeconds, setStuckMinSeconds };
}
