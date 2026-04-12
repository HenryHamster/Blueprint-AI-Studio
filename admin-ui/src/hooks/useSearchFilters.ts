import { useState } from 'react';

export function useSearchFilters() {
  const [searchQuery, setSearchQuery] = useState('');
  const [taskTypeFilter, setTaskTypeFilter] = useState<string[]>([
    'create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor',
  ]);
  const [statusFilter, setStatusFilter] = useState<string[]>([
    'QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED',
  ]);

  return { searchQuery, setSearchQuery, taskTypeFilter, setTaskTypeFilter, statusFilter, setStatusFilter };
}
