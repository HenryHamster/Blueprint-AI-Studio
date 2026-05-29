import { afterEach, beforeEach, describe, expect, test } from 'vitest';
import { page } from 'vitest/browser';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { BoardSavedViewsBar, type BoardFilterSnapshot } from './BoardSavedViewsBar';
import { saveBoardSavedViews, type BoardSavedView } from './boardSavedViews';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const USER = 'operator@example.com';
const PROJECT = 'proj-viewport';

// A filter snapshot that diverges from the default so the active tab renders its
// "modified" dot — exercises the densest single-tab layout path.
const MODIFIED_SNAPSHOT: BoardFilterSnapshot = {
  query: 'token-refresh sliding expiry',
  taskTypeFilter: ['create', 'fix'],
  statusFilter: ['FAILED'],
  dateRange: '24h',
  blocked: true,
  requiresApproval: true,
  stuck: true,
  stuckMinSeconds: 3600,
  failureClassFilter: ['CODE_ERROR'],
  sortBy: 'oldest',
};

function makeView(id: string, name: string): BoardSavedView {
  return {
    id,
    name,
    description: 'Pinned triage view',
    query: 'auth',
    taskTypeFilter: ['create', 'fix'],
    statusFilter: ['FAILED'],
    dateRange: '24h',
    blocked: true,
    requiresApproval: false,
    stuck: true,
    stuckMinSeconds: 3600,
    failureClassFilter: ['CODE_ERROR'],
    sortBy: 'oldest',
    createdAt: 1000,
  };
}

describe('BoardSavedViewsBar — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
    localStorage.clear();
  });

  // Mount the bar. `views` are seeded into localStorage before the component's
  // useState initializer loads them. Keep seeded names short here: the tab strip
  // is intentionally horizontally scrollable (overflow-x-auto), so stressing it
  // with overflowing content is covered separately by the scroll-clipping test
  // below — these assertion helpers verify the non-scrolling base layout.
  function mount(views: BoardSavedView[] = []) {
    if (views.length > 0) saveBoardSavedViews(USER, PROJECT, views);
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    return act(async () => {
      r.render(
        <BoardSavedViewsBar
          userId={USER}
          projectId={PROJECT}
          current={MODIFIED_SNAPSHOT}
          onApply={() => {}}
        />,
      );
    });
  }

  test('renders the base bar (Default tab + Save view) without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, () => mount());
  });

  test('renders without horizontal overflow at 768px (md: breakpoint — Save view label appears)', async () => {
    await expectNoOverflowAt(768, () => mount());
  });

  test('tab and control labels are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, () => mount([makeView('v1', 'Auth'), makeView('v2', 'Stuck')]));
  });

  test('the Save view control meets the 44px touch target at 375px', async () => {
    // Tab/kebab buttons live inside the strip (data-viewport-allow-small-target);
    // the always-visible Save view button is the operator-critical touch target.
    await expectMinTouchTargetAt(375, () => mount([makeView('v1', 'Auth')]));
  });

  test('a long, scrollable tab strip clips horizontally instead of widening the page at 375px', async () => {
    await page.viewport(375, 800);
    await mount([
      makeView('v1', 'Unresolved blockers from autonomous fix-loop iterations across directions'),
      makeView('v2', 'Pending approval — strategic payload-bloat stalls'),
      makeView('v3', 'Stuck over one hour on the slow remediation pipeline'),
    ]);
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));

    // The strip scrolls its own overflow; the document must never widen past the
    // viewport (the real regression: a strip missing min-w-0/overflow-x-auto).
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(376);

    const strip = host!.querySelector<HTMLElement>('[data-testid="board-saved-views-tab-strip"]');
    expect(strip).not.toBeNull();
    // The strip container itself (not its scrolled-away children) stays in bounds.
    expect(strip!.getBoundingClientRect().right).toBeLessThanOrEqual(376);
  });
});
