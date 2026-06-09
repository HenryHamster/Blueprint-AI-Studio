import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps } from 'react';
import { QuickFilterPresets } from './QuickFilterPresets';
import { STATUS_OPTIONS } from '../constants';
import { FILTER_TASK_TYPE_IDS } from '../taskCapabilities';
import type { DateRange } from '../hooks/useSearchFilters';

const ALL_STATUSES = STATUS_OPTIONS.map(o => o.value);

function makeProps(overrides: Partial<ComponentProps<typeof QuickFilterPresets>> = {}) {
  return {
    statusFilter: ALL_STATUSES,
    dateRange: 'all' as DateRange,
    requiresApproval: false,
    blockedFilter: false,
    stuckFilter: false,
    stuckCount: 0,
    stuckCount1h: 0,
    stuckMinSeconds: 0,
    searchQuery: '',
    failureClassFilter: [] as string[],
    setStatusFilter: vi.fn(),
    setTaskTypeFilter: vi.fn(),
    setDateRange: vi.fn(),
    setRequiresApproval: vi.fn(),
    setBlockedFilter: vi.fn(),
    setStuckFilter: vi.fn(),
    setStuckMinSeconds: vi.fn(),
    setSearchQuery: vi.fn(),
    setFailureClassFilter: vi.fn(),
    ...overrides,
  };
}

describe('QuickFilterPresets', () => {
  it('applies the Parked preset and clears conflicting filters', async () => {
    const user = userEvent.setup();
    const props = makeProps({
      statusFilter: ['FAILED'],
      dateRange: '24h',
      requiresApproval: true,
      blockedFilter: true,
      stuckFilter: true,
      stuckMinSeconds: 3600,
      searchQuery: 'stale',
      failureClassFilter: ['CODE_ERROR'],
    });
    render(<QuickFilterPresets {...props} />);

    await user.click(screen.getByRole('button', { name: 'Parked' }));

    expect(props.setStatusFilter).toHaveBeenCalledWith(['PARKED']);
    expect(props.setTaskTypeFilter).toHaveBeenCalledWith([...FILTER_TASK_TYPE_IDS]);
    expect(props.setDateRange).toHaveBeenCalledWith('all');
    expect(props.setRequiresApproval).toHaveBeenCalledWith(false);
    expect(props.setBlockedFilter).toHaveBeenCalledWith(false);
    expect(props.setStuckFilter).toHaveBeenCalledWith(false);
    expect(props.setStuckMinSeconds).toHaveBeenCalledWith(0);
    expect(props.setSearchQuery).toHaveBeenCalledWith('');
    expect(props.setFailureClassFilter).toHaveBeenCalledWith([]);
  });

  it('toggles an active Parked preset back to all statuses', async () => {
    const user = userEvent.setup();
    const props = makeProps({ statusFilter: ['PARKED'] });
    render(<QuickFilterPresets {...props} />);

    await user.click(screen.getByRole('button', { name: 'Parked' }));

    expect(props.setStatusFilter).toHaveBeenCalledWith(ALL_STATUSES);
    expect(props.setFailureClassFilter).toHaveBeenCalledWith([]);
  });
});
