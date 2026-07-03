import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CommandPalette, type WorkbenchCommand } from './CommandPalette';
import { expectMinTouchTargetAt, expectNoOverflowAt } from '../test/viewport';

const viewportCommands: WorkbenchCommand[] = [
  {
    id: 'open-long-task',
    label: 'Open strategic review task with unusually long operator-facing title',
    section: 'Navigation',
    detail: 'Jump to the active task drawer without losing the current workbench context',
    keywords: ['drawer', 'task', 'review'],
    shortcut: 'Ctrl+K O',
    run: () => {},
  },
  {
    id: 'sync-fleet',
    label: 'Sync fleet capacity and runner health',
    section: 'Operations',
    detail: 'Refresh all pool accounts, warm runners, and capacity pressure signals',
    keywords: ['runner', 'capacity', 'pool'],
    shortcut: 'Ctrl+Shift+S',
    run: () => {},
  },
  {
    id: 'disabled-release',
    label: 'Promote release candidate after guarded verification',
    section: 'Release',
    detail: 'Disabled until verification evidence reaches the promotion threshold',
    keywords: ['release', 'promote'],
    disabled: true,
    shortcut: 'Ctrl+P',
    run: () => {},
  },
];

describe('CommandPalette viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

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
  });

  function mount(query = 'runner') {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    act(() => {
      r.render(
        <CommandPalette
          open
          commands={viewportCommands}
          query={query}
          onQueryChange={() => {}}
          onClose={() => {}}
        />,
      );
    });
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, () => {
      mount('');
    });
  });

  test('interactive controls meet 44px targets at 375px', async () => {
    await expectMinTouchTargetAt(375, () => {
      mount('runner');
    });
  });
});
