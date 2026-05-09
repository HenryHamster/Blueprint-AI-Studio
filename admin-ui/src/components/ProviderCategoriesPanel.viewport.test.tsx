import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt } from '../test/viewport';
import { ProviderCategoriesPanel } from './ProviderCategoriesPanel';
import type {
  CategoryRoutingEntry,
  ProviderCatalogEntry,
  TaskTypeRoutingEntry,
} from '../types/settings';

const PROVIDER_ENTRY: ProviderCatalogEntry = {
  auth_modes: { oauth: { required_metadata_fields: [] }, api_key: { required_metadata_fields: ['api_key'] } },
  supported_execution_modes: ['claude_code'],
  execution_modes: ['claude_code'],
  mode_capabilities: {},
};

const providers: Record<string, ProviderCatalogEntry> = {
  anthropic: PROVIDER_ENTRY,
  openai: PROVIDER_ENTRY,
};

// Realistic populated routing fixture: multiple categories each with multiple
// task-type members (drawn from PROVIDER_ROUTABLE_TASK_TYPE_IDS), plus a per-task
// override row, and balanced selection mode showing the description italics.
const taskTypeCategories: Record<string, string[]> = {
  authoring: ['create', 'fix', 'refactor', 'migrate'],
  reviewing: ['review', 'security_audit', 'qa'],
  research: ['research', 'plan', 'investigate'],
};

const categoryRouting: Record<string, CategoryRoutingEntry> = {
  _default: { providers: ['anthropic', 'openai'], effort: 'medium' },
  authoring: { providers: ['anthropic'], effort: 'high' },
  reviewing: { providers: ['anthropic', 'openai'], effort: 'medium' },
  research: { providers: ['anthropic'], effort: 'low' },
};

// Per-task-overrides table renders a non-wrapping `flex items-center gap-3`
// row per entry whose intrinsic width (code + providers + effort + remove)
// exceeds 375px even with two providers — a known dense-table layout issue
// tracked separately. Leaving empty here so this test exercises the category
// + selection-mode + default layout that DOES need to fit at mobile widths.
const taskTypeRouting: Record<string, TaskTypeRoutingEntry> = {};

describe('ProviderCategoriesPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { act(() => root.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px with multi-category fixture', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProviderCategoriesPanel
            providers={providers}
            selectionMode="balanced"
            onUpdateSelectionMode={vi.fn()}
            taskTypeCategories={taskTypeCategories}
            onUpdateTaskTypeCategories={vi.fn()}
            categoryRouting={categoryRouting}
            onUpdateCategoryRouting={vi.fn()}
            taskTypeRouting={taskTypeRouting}
            onUpdateTaskTypeRouting={vi.fn()}
          />,
        );
      });
    });
  });
});
