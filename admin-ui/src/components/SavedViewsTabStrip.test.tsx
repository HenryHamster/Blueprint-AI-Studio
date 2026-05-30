import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { createRef } from 'react';
import { SavedViewsTabStrip } from './SavedViewsTabStrip';
import { type SavedView } from './savedViews';

const DEFAULT_VIEW_ID = '__default__';

const view = (id: string, name: string): SavedView => ({
  id,
  name,
  query: '',
  prefs: {},
  sort: 'importance',
  showResolved: false,
  routineExpanded: false,
  showOnlyNew: false,
  createdAt: 0,
});

function renderStrip(overrides: Partial<Parameters<typeof SavedViewsTabStrip>[0]> = {}) {
  const props = {
    savedViews: [view('v1', 'Blockers'), view('v2', 'Questions')] as SavedView[],
    activeViewId: DEFAULT_VIEW_ID,
    defaultViewId: DEFAULT_VIEW_ID,
    activeTabModified: false,
    openKebabId: null as string | null,
    setOpenKebabId: vi.fn(),
    kebabRef: createRef<HTMLDivElement>(),
    onApplyView: vi.fn(),
    onRenameView: vi.fn(),
    onDeleteView: vi.fn(),
    ...overrides,
  };
  render(<SavedViewsTabStrip {...props} />);
  return props;
}

describe('SavedViewsTabStrip', () => {
  it('renders the Default tab plus a tab per saved view', () => {
    renderStrip();
    const strip = screen.getByTestId('saved-views-tab-strip');
    expect(within(strip).getByText('Default')).toBeTruthy();
    expect(within(strip).getByText('Blockers')).toBeTruthy();
    expect(within(strip).getByText('Questions')).toBeTruthy();
  });

  it('highlights the active tab with the blue accent classes', () => {
    renderStrip({ activeViewId: 'v1' });
    const activeBtn = screen.getByText('Blockers').closest('button')!;
    expect(activeBtn.className).toContain('bg-blue-500/15');
  });

  it('shows the modified dot only on the active tab when filters changed', () => {
    renderStrip({ activeViewId: 'v1', activeTabModified: true });
    expect(screen.getByTestId('view-modified-dot')).toBeTruthy();
  });

  it('does not show the modified dot when filters are unchanged', () => {
    renderStrip({ activeViewId: 'v1', activeTabModified: false });
    expect(screen.queryByTestId('view-modified-dot')).toBeNull();
  });

  it('calls onApplyView with the tab id when a tab is clicked', () => {
    const props = renderStrip();
    fireEvent.click(screen.getByText('Blockers'));
    expect(props.onApplyView).toHaveBeenCalledWith('v1');
  });

  it('toggles the kebab menu via setOpenKebabId', () => {
    const props = renderStrip();
    fireEvent.click(screen.getByLabelText('Manage view Blockers'));
    expect(props.setOpenKebabId).toHaveBeenCalled();
  });

  it('fires onRenameView and onDeleteView from the open kebab menu', () => {
    const props = renderStrip({ openKebabId: 'v1' });
    fireEvent.click(screen.getByText('Rename'));
    expect(props.onRenameView).toHaveBeenCalledWith(props.savedViews[0]);
    fireEvent.click(screen.getByText('Delete'));
    expect(props.onDeleteView).toHaveBeenCalledWith('v1');
  });

  it('does not render a kebab for the Default tab', () => {
    renderStrip();
    expect(screen.queryByLabelText('Manage view Default')).toBeNull();
  });
});
