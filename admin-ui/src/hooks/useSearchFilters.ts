import { useState } from 'react';
import { FILTER_TASK_TYPE_IDS } from '../taskCapabilities';

export type DateRange = 'all' | '1h' | '6h' | '24h' | '7d';

export function useSearchFilters() {
  const [searchQuery, setSearchQuery] = useState('');
  const [taskTypeFilter, setTaskTypeFilter] = useState<string[]>([...FILTER_TASK_TYPE_IDS]);
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
