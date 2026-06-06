import { describe, test, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import { createRef } from 'react';
import { CatchUpNeedsAttentionSection } from './CatchUpNeedsAttentionSection';
import type { NeedsAttentionItem, CatchUpNeedsAttentionSectionProps } from './CatchUpNeedsAttentionSection';

function renderSection(items: NeedsAttentionItem[]) {
  const props: CatchUpNeedsAttentionSectionProps = {
    items,
    visibleItems: items,
    approvalItems: items.filter(i => i.reason === 'approval_required'),
    failedItems: items.filter(i => i.reason === 'failed'),
    dismissed: {},
    expandedSnippets: new Set<string>(),
    openDropdown: null,
    showDismissed: false,
    dropdownRef: createRef<HTMLDivElement>(),
    isHidden: () => false,
    onSelectTask: vi.fn(),
    onApprove: vi.fn(),
    handleApprove: vi.fn(),
    handleRetry: vi.fn(),
    handleBulkApproveAll: vi.fn(),
    handleBulkRetryAll: vi.fn(),
    setOpenDropdown: vi.fn(),
    toggleSnippet: vi.fn(),
    dismissItem: vi.fn(),
    snoozeItem: vi.fn(),
    restoreItem: vi.fn(),
    handleToggleShowDismissed: vi.fn(),
  };
  return render(<CatchUpNeedsAttentionSection {...props} />);
}

const baseApproval: NeedsAttentionItem = {
  task_id: 't1',
  title: 'Pending approval',
  reason: 'approval_required',
  error_snippet: null,
  pr_url: null,
};

describe('CatchUpNeedsAttentionSection held compute launch', () => {
  test('renders proposed spend, deadline, and held compute label', () => {
    const { getByText, getByTitle } = renderSection([
      {
        ...baseApproval,
        task_id: 'compute1',
        title: 'Held compute experiment',
        task_type: 'compute_experiment',
        spend_cap_usd: 12.5,
        deadline_seconds: 7200,
      },
    ]);
    expect(getByText('Held compute launch')).toBeTruthy();
    expect(getByText('spend $12.5')).toBeTruthy();
    expect(getByText('deadline 2h')).toBeTruthy();
    // distinct from a plain approval — purple held-launch chip is present
    expect(getByTitle(/held for operator approval/i)).toBeTruthy();
  });

  test('plain approval entry shows no compute spend/deadline/label', () => {
    const { queryByText } = renderSection([
      { ...baseApproval, task_id: 'plain1', title: 'Plain approval' },
    ]);
    expect(queryByText('Held compute launch')).toBeNull();
    expect(queryByText(/^spend \$/)).toBeNull();
    expect(queryByText(/^deadline /)).toBeNull();
  });

  test('formats sub-hour deadline in minutes', () => {
    const { getByText } = renderSection([
      {
        ...baseApproval,
        task_id: 'compute2',
        task_type: 'compute_experiment',
        spend_cap_usd: 1,
        deadline_seconds: 1800,
      },
    ]);
    expect(getByText('deadline 30m')).toBeTruthy();
  });
});
