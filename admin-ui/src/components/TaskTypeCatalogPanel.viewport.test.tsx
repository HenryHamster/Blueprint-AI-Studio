import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt } from '../test/viewport';
import { TaskTypeCatalogPanel } from './TaskTypeCatalogPanel';
import type { TaskTypeCatalogEntry } from '../api';

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

  async function render(entries: TaskTypeCatalogEntry[]) {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<TaskTypeCatalogPanel entries={entries} />);
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
});
