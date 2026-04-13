import { useState } from 'react';

export type DateRange = 'all' | '1h' | '6h' | '24h' | '7d';

export function useSearchFilters() {
  const [searchQuery, setSearchQuery] = useState('');
  const [taskTypeFilter, setTaskTypeFilter] = useState<string[]>([
    'create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor',
  ]);
  const [statusFilter, setStatusFilter] = useState<string[]>([
    'QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED',
  ]);
  const [dateRange, setDateRange] = useState<DateRange>('all');

  return { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter, dateRange, setDateRange };
}
