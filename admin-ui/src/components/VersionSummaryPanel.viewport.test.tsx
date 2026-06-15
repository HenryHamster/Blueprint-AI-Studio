import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { VersionSummaryPanel } from './VersionSummaryPanel';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import type { ChangelogEntry } from '../api';

const SAMPLE_TASKS: ChangelogEntry[] = [
  { task_type: 'create', title: 'Add VersionSummaryPanel component with changelog grouping by task type', pr_number: 10255 },
  { task_type: 'create', title: 'Expose tasks_merged on CurrentVersion via backend release_version module', pr_number: 10254 },
  { task_type: 'fix', title: 'Fix viewport test coverage for new operator components', pr_number: 10260 },
  { task_type: 'fix', title: 'Resolve mock-signature drift in VersionLifecyclePanel unit tests after API extension', pr_number: 10261 },
  { task_type: 'research', title: 'Truthful state layer: canonical backend module for system/version/deploy state', pr_number: null },
];

describe('VersionSummaryPanel — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
      });
    });
  });

  test('task titles are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
      });
    });
  });

  // VersionSummaryPanel is a read-only display panel with no interactive controls.
  test('touch target (vacuous — no interactive controls)', async () => {
    await expectMinTouchTargetAt(
      375,
      async () => {
        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
        });
      },
      { selector: 'none' },
    );
  });
});
