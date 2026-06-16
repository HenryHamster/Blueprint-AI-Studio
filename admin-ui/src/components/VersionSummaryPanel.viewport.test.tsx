import { afterEach, describe, expect, test } from 'vitest';
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
  { task_type: 'create', title: 'Add VersionSummaryPanel component with changelog grouping by task type', pr_number: 10255, pr_url: 'https://github.com/org/repo/pull/10255' },
  { task_type: 'create', title: 'Expose tasks_merged on CurrentVersion via backend release_version module', pr_number: 10254, pr_url: 'https://github.com/org/repo/pull/10254' },
  { task_type: 'fix', title: 'Fix viewport test coverage for new operator components', pr_number: 10260, pr_url: null },
  { task_type: 'fix', title: 'Resolve mock-signature drift in VersionLifecyclePanel unit tests after API extension', pr_number: 10261, pr_url: null },
  { task_type: 'research', title: 'Truthful state layer: canonical backend module for system/version/deploy state', pr_number: null, pr_url: null },
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

  test('renders null when tasksMerged is undefined', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={undefined} />);
    });
    const panel = host.querySelector('[data-testid="version-summary-panel"]');
    const emptyPanel = host.querySelector('[data-testid="version-summary-panel-empty"]');
    if (panel !== null || emptyPanel !== null) {
      throw new Error('Expected no panel rendered when tasksMerged is undefined');
    }
  });

  test('renders empty-state placeholder when tasksMerged is an empty array', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={[]} />);
    });
    const emptyPanel = host.querySelector('[data-testid="version-summary-panel-empty"]');
    if (emptyPanel === null) {
      throw new Error('Expected empty-state panel when tasksMerged is []');
    }
    if (!emptyPanel.textContent?.includes('No tasks merged into this version yet.')) {
      throw new Error('Expected placeholder text in empty-state panel');
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

  test('changelog entry with pr_url renders a clickable anchor to the PR', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
    });
    const anchors = host.querySelectorAll('a[href="https://github.com/org/repo/pull/10255"]');
    expect(anchors.length).toBe(1);
    expect(anchors[0].textContent).toBe('#10255');
    expect(anchors[0].getAttribute('target')).toBe('_blank');
  });

  test('heading prop overrides default label', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} heading="What merged" />);
    });
    const panel = host.querySelector('[data-testid="version-summary-panel"]');
    if (!panel?.textContent?.includes('What merged')) {
      throw new Error('Expected heading "What merged" when heading prop is passed');
    }
    if (panel?.textContent?.includes('What shipped')) {
      throw new Error('Expected default heading "What shipped" to be absent when heading prop overrides it');
    }
  });

  test('omitting heading prop renders default "What shipped" label', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
    });
    const panel = host.querySelector('[data-testid="version-summary-panel"]');
    if (!panel?.textContent?.includes('What shipped')) {
      throw new Error('Expected default heading "What shipped" when heading prop is omitted');
    }
  });

  test('changelog entry with null pr_url renders plain text PR number', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
    });
    // pr_number=10260 has pr_url=null — should be a <span>, not an <a>
    const spans = Array.from(host.querySelectorAll('span')).filter(
      (el) => el.textContent === '#10260',
    );
    expect(spans.length).toBe(1);
  });
});
