import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SearchFilterBar } from './SearchFilterBar';

describe('SearchFilterBar', () => {
  it('keeps export actions out of the default task filter bar', () => {
    render(
      <SearchFilterBar
        searchQuery=""
        onSearchChange={vi.fn()}
        taskTypeFilter={['create']}
        onTypeFilterChange={vi.fn()}
        dateRange="all"
        onDateRangeChange={vi.fn()}
        blockedFilter={false}
        onBlockedFilterChange={vi.fn()}
      />,
    );

    expect(screen.getByPlaceholderText('Search by title, ID, or status...')).toBeInTheDocument();
    expect(screen.queryByTitle('Download CSV')).not.toBeInTheDocument();
  });
});
