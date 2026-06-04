import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt } from '../test/viewport';
import { TaskTypeCatalogPanel } from './TaskTypeCatalogPanel';
import type { TaskTypeCatalogEntry, TaskTypeCostAdherenceEntry } from '../api';

const ENTRIES: TaskTypeCatalogEntry[] = [
  {
    id: 'create',
    label: 'Create',
    description: 'Implement new functionality and open a PR.',
    agent_mode: 'write',
    default_category: 'build',
    user_launchable: true,
    provider_configurable: true,
    filter_visible: true,
    output_kind: 'pr',
    requires_code: true,
    requires_web: false,
    requires_compute: false,
    evaluation_strategy: 'ci_and_review',
    cost_ceiling_usd: 8.0,
  },
  {
    id: 'research',
    label: 'Research',
    description: 'Analyze the codebase and produce findings that the strategic loop consumes to decide the next direction.',
    agent_mode: 'read_only',
    default_category: 'strategic',
    user_launchable: true,
    provider_configurable: true,
    filter_visible: true,
    output_kind: 'knowledge',
    requires_code: true,
    requires_web: true,
    requires_compute: false,
    evaluation_strategy: 'forward_quality',
    cost_ceiling_usd: 4.0,
  },
  {
    id: 'memo',
    label: 'Memo',
    description: 'Terminal reasoning artifact persisted without opening a PR.',
    agent_mode: 'read_only',
    default_category: 'strategic',
    user_launchable: false,
    provider_configurable: true,
    filter_visible: true,
    output_kind: 'artifact',
    requires_code: false,
    requires_web: false,
    requires_compute: false,
    evaluation_strategy: 'none',
    cost_ceiling_usd: null,
  },
];

describe('TaskTypeCatalogPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { act(() => root.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function render(
    entries: TaskTypeCatalogEntry[],
    adherence?: TaskTypeCostAdherenceEntry[],
  ) {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<TaskTypeCatalogPanel entries={entries} adherence={adherence} />);
    });
  }

  test('renders the catalog grid without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        // Expand by default so the grid is laid out under the 375px gate.
        localStorage.setItem('blueprint-admin:taskTypeCatalogCollapsed', 'false');
        r.render(<TaskTypeCatalogPanel entries={ENTRIES} />);
      });
    });
  });

  test('renders one card per injected entry with a launchable badge only on launchable types', async () => {
    localStorage.setItem('blueprint-admin:taskTypeCatalogCollapsed', 'false');
    await render(ENTRIES);
    const cards = host!.querySelectorAll('[data-testid="task-type-card"]');
    expect(cards.length).toBe(ENTRIES.length);
    const badges = host!.querySelectorAll('[data-testid="launchable-badge"]');
    // create + research are launchable; memo is not.
    expect(badges.length).toBe(2);
    // Cost ceiling and evaluation strategy surface for the create card.
    const createCard = host!.querySelector('[data-task-type="create"]');
    expect(createCard?.textContent).toContain('$8.00');
    expect(createCard?.textContent).toContain('CI + review');
  });

  test('renders actual avg/p95 cost and an over-ceiling badge when a type chronically overruns', async () => {
    localStorage.setItem('blueprint-admin:taskTypeCatalogCollapsed', 'false');
    const adherence: TaskTypeCostAdherenceEntry[] = [
      {
        id: 'create',
        declared_ceiling_usd: 8.0,
        avg_cost_usd: 12.5,
        p95_cost_usd: 18.0,
        sample_count: 7,
        over_ceiling: true,
      },
      // research has no in-window samples -> actual cost degrades to "—".
      {
        id: 'research',
        declared_ceiling_usd: 4.0,
        avg_cost_usd: null,
        p95_cost_usd: null,
        sample_count: 0,
        over_ceiling: false,
      },
    ];
    await render(ENTRIES, adherence);

    const createCard = host!.querySelector('[data-task-type="create"]');
    expect(createCard?.textContent).toContain('$12.50');
    expect(createCard?.textContent).toContain('$18.00');
    // Over-ceiling types get a visual warning badge.
    expect(createCard?.querySelector('[data-testid="over-ceiling-badge"]')).not.toBeNull();

    // A type with zero samples shows no badge and a blank actual cost.
    const researchCard = host!.querySelector('[data-task-type="research"]');
    expect(researchCard?.querySelector('[data-testid="over-ceiling-badge"]')).toBeNull();
    const researchActual = researchCard?.querySelector('[data-testid="actual-cost"]');
    expect(researchActual?.textContent?.trim()).toBe('—');

    // memo has no adherence entry at all -> sample column also blank.
    const memoCard = host!.querySelector('[data-task-type="memo"]');
    expect(memoCard?.querySelector('[data-testid="actual-cost"]')?.textContent?.trim()).toBe('—');
  });
});
