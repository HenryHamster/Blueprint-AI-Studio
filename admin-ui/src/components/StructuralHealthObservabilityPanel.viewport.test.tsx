import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StructuralHealthObservabilityPanel } from './StructuralHealthObservabilityPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    fetchStructuralHealthObservability: vi.fn(async () => ({
      cycles: 3,
      enrolled_total: 2,
      last_decision: { action: 'observe', check: 'fat_row', enrolled: 0 },
      largest_rows: [
        { task_id: 'aaaa-bbbb-cccc-dddd', project_id: 'proj-1111-2222-3333', bytes: 2_097_152 },
        { task_id: 'eeee-ffff-0000-1111', project_id: 'proj-1111-2222-3333', bytes: 1_048_576 },
      ],
      recent: [
        { action: 'observe', check: 'fat_row', enrolled: 0 },
        { action: 'enroll', check: 'fat_row', enrolled: 1 },
      ],
      last_jsonb_bloat_decision: null,
      last_gin_overhead_decision: null,
      last_pool_saturation_decision: null,
    })),
  };
});

describe('StructuralHealthObservabilityPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<StructuralHealthObservabilityPanel />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });

  test('renders without horizontal overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<StructuralHealthObservabilityPanel />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });
});
