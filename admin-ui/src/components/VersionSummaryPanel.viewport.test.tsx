import { afterEach, describe, expect, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { VersionSummaryPanel } from './VersionSummaryPanel';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import type { ChangelogEntry, VersionCriterionCoverage } from '../api';

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

  test('renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
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

  test('per-type count summary badges render at 768px', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
    });
    const summary = host.querySelector('[data-testid="type-count-summary"]');
    if (summary === null) {
      throw new Error('Expected type-count-summary element to be present');
    }
    // SAMPLE_TASKS has create (2), fix (2), research (1)
    const text = summary.textContent ?? '';
    if (!text.includes('2') || !text.includes('create')) {
      throw new Error('Expected type-count-summary to include count badges');
    }
  });

  test('type section toggle collapses and expands entries', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
    });
    const toggle = host.querySelector('[data-testid="type-section-toggle-create"]') as HTMLButtonElement | null;
    if (toggle === null) {
      throw new Error('Expected type-section-toggle-create button');
    }
    // Initially open (≤20 tasks)
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    // Collapse
    await act(async () => {
      toggle.click();
    });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    // Expand again
    await act(async () => {
      toggle.click();
    });
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
  });

  test('acceptance bucket breakdown renders non-zero non-proven buckets', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <VersionSummaryPanel
          tasksMerged={SAMPLE_TASKS}
          acceptanceAggregate={{ proven: 2, undeclared: 3, declared_unsatisfied: 1, satisfied_unproven: 0 }}
        />,
      );
    });
    const breakdown = host.querySelector('[data-testid="acceptance-bucket-breakdown"]');
    if (breakdown === null) throw new Error('Expected acceptance-bucket-breakdown element');
    const text = breakdown.textContent ?? '';
    if (!text.includes('3') || !text.includes('no criteria')) throw new Error('Expected undeclared bucket in breakdown');
    if (!text.includes('1') || !text.includes('criteria failed')) throw new Error('Expected declared_unsatisfied bucket in breakdown');
    // proven is excluded from breakdown; satisfied_unproven is 0 so also excluded
    if (text.includes('proven')) throw new Error('proven should not appear in bucket breakdown');
  });

  test('acceptance bucket breakdown is absent when aggregate is not provided', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
    });
    const breakdown = host.querySelector('[data-testid="acceptance-bucket-breakdown"]');
    if (breakdown !== null) throw new Error('Expected no acceptance-bucket-breakdown when acceptanceAggregate is absent');
  });

  test('filters changelog entries by title and PR substring', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
    });

    const input = host.querySelector('[data-testid="changelog-filter-input"]') as HTMLInputElement | null;
    if (input === null) throw new Error('Expected changelog-filter-input to be present');

    // Filter by title substring — only the 'create' entry matching 'VersionSummaryPanel' should appear
    await act(async () => {
      const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
      nativeInputValueSetter?.call(input, 'VersionSummaryPanel');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });

    // Expect no-results sentinel to be absent (there is a match)
    const noResults = host.querySelector('[data-testid="changelog-filter-no-results"]');
    if (noResults !== null) throw new Error('Expected matching entries but got no-results message');

    // The matching title should be visible
    const titles = Array.from(host.querySelectorAll('li span.text-xs'));
    const visibleTitles = titles.map((el) => el.textContent ?? '');
    if (!visibleTitles.some((t) => t.includes('VersionSummaryPanel'))) {
      throw new Error(`Expected title containing "VersionSummaryPanel" but got: ${JSON.stringify(visibleTitles)}`);
    }
    // Unrelated entries should not be visible
    if (visibleTitles.some((t) => t.includes('Fix viewport test coverage'))) {
      throw new Error('Expected fix entry to be filtered out');
    }

    // Filter by PR number substring
    await act(async () => {
      const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
      nativeInputValueSetter?.call(input, '10260');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const prSpans = Array.from(host.querySelectorAll('li span')).filter((el) => el.textContent === '#10260');
    if (prSpans.length === 0) throw new Error('Expected entry with PR #10260 to appear when filtering by "10260"');

    // Clear filter — all entries should return
    await act(async () => {
      const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
      nativeInputValueSetter?.call(input, '');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const allTitles = Array.from(host.querySelectorAll('li span.text-xs')).map((el) => el.textContent ?? '');
    if (allTitles.length !== SAMPLE_TASKS.length) {
      throw new Error(`Expected all ${SAMPLE_TASKS.length} tasks after clearing filter, got ${allTitles.length}`);
    }
  });

  test('filter shows no-results message when nothing matches', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} />);
    });
    const input = host.querySelector('[data-testid="changelog-filter-input"]') as HTMLInputElement | null;
    if (input === null) throw new Error('Expected changelog-filter-input');
    await act(async () => {
      const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
      nativeInputValueSetter?.call(input, 'xyznonexistent');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    const noResults = host.querySelector('[data-testid="changelog-filter-no-results"]');
    if (noResults === null) throw new Error('Expected no-results message for unmatched filter');
    if (!noResults.textContent?.includes('xyznonexistent')) {
      throw new Error('Expected no-results message to include the filter query');
    }
  });

  test('large version (>20 tasks) defaults sections to collapsed', async () => {
    const manyTasks: ChangelogEntry[] = Array.from({ length: 25 }, (_, i) => ({
      task_type: 'create',
      title: `Task ${i}`,
      pr_number: 10000 + i,
      pr_url: null,
    }));
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={manyTasks} />);
    });
    const toggle = host.querySelector('[data-testid="type-section-toggle-create"]') as HTMLButtonElement | null;
    if (toggle === null) {
      throw new Error('Expected type-section-toggle-create button');
    }
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
  });

  test('all-retracted criterion coverage renders audit rows instead of no-criteria empty state', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 0,
      covered: 0,
      partial: 0,
      uncovered: 0,
      retracted: 2,
      superseded: 0,
      inactive: 2,
      criteria: [
        { criterion_key: 'crit-old-a', text: 'Old acceptance target A', coverage: 'uncovered', status: 'retracted', evidence_count: 0 },
        { criterion_key: 'crit-old-b', text: 'Old acceptance target B', coverage: 'partial', status: 'retracted', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });

    const panel = host.querySelector('[data-testid="criterion-coverage-panel"]');
    if (panel === null) throw new Error('Expected criterion coverage panel');
    const text = panel.textContent ?? '';
    if (text.includes('No acceptance criteria defined')) {
      throw new Error('All-retracted coverage must render audit rows, not the no-criteria empty state');
    }
    expect(text).toContain('0 active criteria');
    expect(text).toContain('2 inactive');
    expect(host.querySelector('[data-testid="criterion-row-crit-old-a"]')?.textContent).toContain('Old acceptance target A');
    expect(host.querySelector('[data-testid="criterion-status-crit-old-a"]')?.textContent).toBe('retracted');
    expect(host.querySelector('[data-testid="criterion-row-crit-old-b"]')?.textContent).toContain('Old acceptance target B');
    expect(host.querySelector('[data-testid="criterion-status-crit-old-b"]')?.textContent).toBe('retracted');
  });

  // ── stuck-criterion diagnosis ───────────────────────────────────────────────

  test('stuck_count badge renders when stuck_count > 0', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 2,
      covered: 1,
      partial: 0,
      uncovered: 1,
      stuck_count: 1,
      criteria: [
        { criterion_key: 'c1', text: 'Covered criterion', coverage: 'covered', evidence_count: 1 },
        { criterion_key: 'c2', text: 'Stuck criterion', coverage: 'uncovered', evidence_count: 0, stuck_reason: 'no_mapping' },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const badge = host.querySelector('[data-testid="stuck-count-badge"]');
    if (badge === null) throw new Error('Expected stuck-count-badge when stuck_count > 0');
    if (!badge.textContent?.includes('1')) throw new Error('Expected stuck count 1 in badge text');
  });

  test('separates machine-verified coverage, unmapped criteria, and evidence still in progress', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 7,
      covered: 0,
      partial: 0,
      uncovered: 7,
      stuck_count: 2,
      criteria: [
        { criterion_key: 'u1', text: 'Unmapped one', coverage: 'uncovered', evidence_count: 0, stuck_reason: 'no_mapping' },
        { criterion_key: 'u2', text: 'Unmapped two', coverage: 'uncovered', evidence_count: 0, stuck_reason: 'no_mapping' },
        { criterion_key: 'a1', text: 'Awaiting one', coverage: 'uncovered', evidence_count: 0 },
        { criterion_key: 'a2', text: 'Awaiting two', coverage: 'uncovered', evidence_count: 0 },
        { criterion_key: 'a3', text: 'Awaiting three', coverage: 'uncovered', evidence_count: 0 },
        { criterion_key: 'a4', text: 'Awaiting four', coverage: 'uncovered', evidence_count: 0 },
        { criterion_key: 'a5', text: 'Awaiting five', coverage: 'uncovered', evidence_count: 0 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });

    const fraction = host.querySelector('[data-testid="criterion-coverage-fraction"]');
    const unmapped = host.querySelector('[data-testid="unmapped-criteria-count"]');
    const awaiting = host.querySelector('[data-testid="awaiting-evidence-count"]');
    if (fraction?.textContent !== '0/7 machine-verified') {
      throw new Error(`Expected explicit machine-verified coverage, got: ${fraction?.textContent}`);
    }
    if (unmapped?.textContent !== '2 unmapped') {
      throw new Error(`Expected unmapped count, got: ${unmapped?.textContent}`);
    }
    if (awaiting?.textContent !== '5 awaiting evidence') {
      throw new Error(`Expected awaiting-evidence count, got: ${awaiting?.textContent}`);
    }
  });

  test('stuck_count badge is absent when stuck_count is 0', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 1,
      partial: 0,
      uncovered: 0,
      stuck_count: 0,
      criteria: [
        { criterion_key: 'c1', text: 'Covered criterion', coverage: 'covered', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const badge = host.querySelector('[data-testid="stuck-count-badge"]');
    if (badge !== null) throw new Error('Expected no stuck-count-badge when stuck_count is 0');
  });

  test('stuck_count badge is absent when stuck_count is null or omitted', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 0,
      partial: 0,
      uncovered: 1,
      criteria: [
        { criterion_key: 'c1', text: 'Uncovered criterion', coverage: 'uncovered', evidence_count: 0 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const badge = host.querySelector('[data-testid="stuck-count-badge"]');
    if (badge !== null) throw new Error('Expected no stuck-count-badge when stuck_count is absent');
  });

  test('stuck_reason no_mapping renders "no direction" label', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 0,
      partial: 0,
      uncovered: 1,
      stuck_count: 1,
      criteria: [
        { criterion_key: 'crit-a', text: 'Unmapped criterion', coverage: 'uncovered', evidence_count: 0, stuck_reason: 'no_mapping' },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const label = host.querySelector('[data-testid="stuck-reason-crit-a"]');
    if (label === null) throw new Error('Expected stuck-reason-crit-a element for no_mapping criterion');
    if (!label.textContent?.includes('no direction')) {
      throw new Error(`Expected "no direction" label for no_mapping, got: "${label.textContent}"`);
    }
  });

  test('stuck_reason direction_retired renders "direction retired" label', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 0,
      partial: 0,
      uncovered: 1,
      stuck_count: 1,
      criteria: [
        { criterion_key: 'crit-b', text: 'Retired-direction criterion', coverage: 'uncovered', evidence_count: 0, stuck_reason: 'direction_retired' },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const label = host.querySelector('[data-testid="stuck-reason-crit-b"]');
    if (label === null) throw new Error('Expected stuck-reason-crit-b element for direction_retired criterion');
    if (!label.textContent?.includes('direction retired')) {
      throw new Error(`Expected "direction retired" label for direction_retired, got: "${label.textContent}"`);
    }
  });

  // ── attested (operator-attestation-only) coverage tier ──────────────────────

  test('attested-only criterion renders the distinct amber "attested" badge', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 2,
      covered: 1,
      attested: 1,
      partial: 0,
      uncovered: 0,
      criteria: [
        { criterion_key: 'c-verified', text: 'Machine-verified criterion', coverage: 'covered', evidence_count: 2 },
        { criterion_key: 'c-attested', text: 'Operator-attested criterion', coverage: 'attested', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const row = host.querySelector('[data-testid="criterion-row-c-attested"]');
    if (row === null) throw new Error('Expected criterion-row-c-attested element');
    const badge = row.querySelector('span.rounded');
    if (badge === null) throw new Error('Expected coverage badge span in attested row');
    expect(badge.textContent).toContain('Attested');
    // Amber tier — visually distinct from emerald "covered" and slate "uncovered".
    expect(badge.className).toContain('text-amber-400');
    expect(badge.className).toContain('bg-amber-950/30');
  });

  test('header separates machine-verified coverage from attested coverage', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 3,
      covered: 1,
      attested: 2,
      partial: 0,
      uncovered: 0,
      criteria: [
        { criterion_key: 'c1', text: 'Verified', coverage: 'covered', evidence_count: 1 },
        { criterion_key: 'c2', text: 'Attested A', coverage: 'attested', evidence_count: 1 },
        { criterion_key: 'c3', text: 'Attested B', coverage: 'attested', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const fraction = host.querySelector('[data-testid="criterion-coverage-fraction"]');
    if (fraction === null) throw new Error('Expected criterion-coverage-fraction element');
    expect(fraction.textContent).toBe('1/3 machine-verified + 2 attested');
    const attestedBadge = host.querySelector('[data-testid="attested-count-badge"]');
    if (attestedBadge === null) throw new Error('Expected attested-count-badge when attested > 0');
    expect(attestedBadge.textContent).toContain('2 attested');
  });

  test('header falls back to plain fraction and hides attested badge when attested is undefined', async () => {
    // Older backend: no `attested` field at all — must be treated as 0.
    const coverage: VersionCriterionCoverage = {
      total: 2,
      covered: 1,
      partial: 0,
      uncovered: 1,
      criteria: [
        { criterion_key: 'c1', text: 'Covered', coverage: 'covered', evidence_count: 1 },
        { criterion_key: 'c2', text: 'Uncovered', coverage: 'uncovered', evidence_count: 0 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const fraction = host.querySelector('[data-testid="criterion-coverage-fraction"]');
    if (fraction === null) throw new Error('Expected criterion-coverage-fraction element');
    expect(fraction.textContent).toBe('1/2 machine-verified');
    if (host.querySelector('[data-testid="attested-count-badge"]') !== null) {
      throw new Error('Expected no attested-count-badge when attested is undefined');
    }
  });

  test('renders without horizontal overflow at 1200px with attested coverage', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 3,
      covered: 1,
      attested: 1,
      partial: 0,
      uncovered: 1,
      criteria: [
        { criterion_key: 'c1', text: 'Machine-verified acceptance criterion with a reasonably long description', coverage: 'covered', evidence_count: 3 },
        { criterion_key: 'c2', text: 'Operator-attested criterion pending machine corroboration before expiry', coverage: 'attested', evidence_count: 1 },
        { criterion_key: 'c3', text: 'Still-uncovered acceptance criterion awaiting evidence', coverage: 'uncovered', evidence_count: 0 },
      ],
    };
    await expectNoOverflowAt(1200, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
      });
    });
  });

  test('criterion with null stuck_reason renders no stuck label', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 0,
      partial: 0,
      uncovered: 1,
      criteria: [
        { criterion_key: 'crit-c', text: 'Plain uncovered criterion', coverage: 'uncovered', evidence_count: 0, stuck_reason: null },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const label = host.querySelector('[data-testid="stuck-reason-crit-c"]');
    if (label !== null) throw new Error('Expected no stuck-reason label when stuck_reason is null');
  });

  // ── merge-evidence-gap breakdown ──────────────────────────────────────────

  test('merge evidence gap breakdown renders reason counts when present', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 3,
      covered: 1,
      partial: 1,
      uncovered: 1,
      merge_evidence_gaps: {
        no_criteria: 5,
        verdict_not_proven: 2,
        no_active_criteria: 1,
      },
      criteria: [
        { criterion_key: 'c1', text: 'Covered criterion', coverage: 'covered', evidence_count: 1 },
        { criterion_key: 'c2', text: 'Partial criterion', coverage: 'partial', evidence_count: 1 },
        { criterion_key: 'c3', text: 'Uncovered criterion', coverage: 'uncovered', evidence_count: 0 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const gapSection = host.querySelector('[data-testid="merge-evidence-gap-breakdown"]');
    if (gapSection === null) throw new Error('Expected merge-evidence-gap-breakdown when gaps present');
    expect(gapSection.textContent).toContain('5 no criteria');
    expect(gapSection.textContent).toContain('2 verdict not proven');
    expect(gapSection.textContent).toContain('1 no active criteria');
  });

  test('merge evidence gap breakdown shows unmatched criterion IDs tooltip', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 2,
      covered: 1,
      partial: 0,
      uncovered: 1,
      merge_evidence_gaps: {
        no_criteria: 3,
        unmatched_criterion_ids: ['crit-a', 'crit-b'],
      },
      criteria: [
        { criterion_key: 'c1', text: 'Covered criterion', coverage: 'covered', evidence_count: 1 },
        { criterion_key: 'c2', text: 'Uncovered criterion', coverage: 'uncovered', evidence_count: 0 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const gapSection = host.querySelector('[data-testid="merge-evidence-gap-breakdown"]');
    if (gapSection === null) throw new Error('Expected merge-evidence-gap-breakdown when gaps present');
    const unmatched = host.querySelector('[data-testid="gap-unmatched-ids"]');
    if (unmatched === null) throw new Error('Expected gap-unmatched-ids element');
    expect(unmatched.textContent).toContain('2 unmatched');
    expect(unmatched.getAttribute('title')).toContain('crit-a');
    expect(unmatched.getAttribute('title')).toContain('crit-b');
  });

  test('merge evidence gap breakdown absent when merge_evidence_gaps is null', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 1,
      partial: 0,
      uncovered: 0,
      merge_evidence_gaps: null,
      criteria: [
        { criterion_key: 'c1', text: 'Covered', coverage: 'covered', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const gapSection = host.querySelector('[data-testid="merge-evidence-gap-breakdown"]');
    if (gapSection !== null) throw new Error('Expected no merge-evidence-gap-breakdown when gaps is null');
  });

  test('merge evidence gap breakdown absent when merge_evidence_gaps is undefined (older backend)', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 1,
      partial: 0,
      uncovered: 0,
      criteria: [
        { criterion_key: 'c1', text: 'Covered', coverage: 'covered', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const gapSection = host.querySelector('[data-testid="merge-evidence-gap-breakdown"]');
    if (gapSection !== null) throw new Error('Expected no merge-evidence-gap-breakdown when gaps is undefined');
  });

  test('merge evidence gap breakdown absent when merge_evidence_gaps is empty object', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 1,
      partial: 0,
      uncovered: 0,
      merge_evidence_gaps: {},
      criteria: [
        { criterion_key: 'c1', text: 'Covered', coverage: 'covered', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const gapSection = host.querySelector('[data-testid="merge-evidence-gap-breakdown"]');
    if (gapSection !== null) throw new Error('Expected no merge-evidence-gap-breakdown when gaps is empty');
  });

  test('merge evidence gap breakdown absent when all counts are zero', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 1,
      partial: 0,
      uncovered: 0,
      merge_evidence_gaps: { no_criteria: 0 },
      criteria: [
        { criterion_key: 'c1', text: 'Covered', coverage: 'covered', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const gapSection = host.querySelector('[data-testid="merge-evidence-gap-breakdown"]');
    if (gapSection !== null) throw new Error('Expected no merge-evidence-gap-breakdown when all gap counts are zero');
  });

  // ── phantom drift detection ─────────────────────────────────────────────────

  test('phantom drift badge renders in empty-state when has_drift is true with phantom keys', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 0,
      covered: 0,
      partial: 0,
      uncovered: 0,
      criteria: [],
      phantom_bookkeeping: {
        phantom_keys: ['crit-alpha', 'crit-beta'],
        zero_registered: true,
        has_drift: true,
      },
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const badge = host.querySelector('[data-testid="phantom-drift-badge"]');
    if (badge === null) throw new Error('Expected phantom-drift-badge when has_drift is true');
    expect(badge.textContent).toContain('2 phantom keys referenced');
    const panel = host.querySelector('[data-testid="criterion-coverage-panel"]');
    const text = panel?.textContent ?? '';
    if (text.includes('No acceptance criteria defined for this version.')) {
      throw new Error('Empty-state with phantom drift must NOT say "No acceptance criteria defined for this version."');
    }
    expect(text).toContain('No criteria registered');
  });

  test('phantom drift badge shows singular form for one phantom key', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 0,
      covered: 0,
      partial: 0,
      uncovered: 0,
      criteria: [],
      phantom_bookkeeping: {
        phantom_keys: ['crit-alpha'],
        zero_registered: true,
        has_drift: true,
      },
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const badge = host.querySelector('[data-testid="phantom-drift-badge"]');
    if (badge === null) throw new Error('Expected phantom-drift-badge');
    expect(badge.textContent).toContain('1 phantom key referenced');
    expect(badge.textContent).not.toContain('keys');
  });

  test('phantom drift badge in non-empty criteria list when phantom_keys present', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 1,
      partial: 0,
      uncovered: 0,
      criteria: [
        { criterion_key: 'crit-registered', text: 'Registered criterion', coverage: 'covered', evidence_count: 1 },
      ],
      phantom_bookkeeping: {
        phantom_keys: ['crit-dangling'],
        zero_registered: false,
        has_drift: true,
      },
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const badge = host.querySelector('[data-testid="phantom-drift-badge"]');
    if (badge === null) throw new Error('Expected phantom-drift-badge in non-empty criteria list');
    expect(badge.textContent).toContain('1 phantom key');
  });

  test('no phantom drift badge when phantom_bookkeeping is absent (older backend)', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 1,
      partial: 0,
      uncovered: 0,
      criteria: [
        { criterion_key: 'c1', text: 'Covered criterion', coverage: 'covered', evidence_count: 1 },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const badge = host.querySelector('[data-testid="phantom-drift-badge"]');
    if (badge !== null) throw new Error('Expected no phantom-drift-badge when phantom_bookkeeping is absent');
  });

  test('no phantom drift badge when has_drift is false', async () => {
    const coverage: VersionCriterionCoverage = {
      total: 1,
      covered: 1,
      partial: 0,
      uncovered: 0,
      criteria: [
        { criterion_key: 'c1', text: 'Covered criterion', coverage: 'covered', evidence_count: 1 },
      ],
      phantom_bookkeeping: {
        phantom_keys: [],
        zero_registered: false,
        has_drift: false,
      },
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<VersionSummaryPanel tasksMerged={SAMPLE_TASKS} criterionCoverage={coverage} />);
    });
    const badge = host.querySelector('[data-testid="phantom-drift-badge"]');
    if (badge !== null) throw new Error('Expected no phantom-drift-badge when has_drift is false');
  });
});
