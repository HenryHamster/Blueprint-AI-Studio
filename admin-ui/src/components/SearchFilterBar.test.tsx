import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SearchFilterBar } from './SearchFilterBar';

describe('SearchFilterBar', () => {
  it('renders the CSV action and invokes the download callback', () => {
    const onDownloadCsv = vi.fn();

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
        onDownloadCsv={onDownloadCsv}
      />,
    );

    fireEvent.click(screen.getByTitle('Download CSV'));
    expect(onDownloadCsv).toHaveBeenCalledTimes(1);
  });
});
