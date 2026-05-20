import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TaskDurationWidget, type TaskDurationStats } from './TaskDurationWidget';
import { expectNoOverflowAt } from '../test/viewport';

const data: TaskDurationStats = {
  stats: {
    create: { p50_seconds: 1840, sample_count: 22 },
    fix: { p50_seconds: 720, sample_count: 17 },
    research: { p50_seconds: 4500, sample_count: 8 },
    review: { p50_seconds: 600, sample_count: 11 },
    refactor: { p50_seconds: 2400, sample_count: 5 },
  },
  scope_duration_stats: {
    small: { p50_seconds: 360, p90_seconds: 900, sample_size: 18 },
    medium: { p50_seconds: 1500, p90_seconds: 3600, sample_size: 14 },
    large: { p50_seconds: 5400, p90_seconds: 9000, sample_size: 4 },
  },
  phase_duration_stats: {
    provisioning: { p50_seconds: 90, sample_size: 36 },
    execution: { p50_seconds: 2100, sample_size: 36 },
  },
};

describe('TaskDurationWidget — viewport regression', () => {
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
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<TaskDurationWidget data={data} />);
      });
    });
  });

  test('renders without horizontal overflow at 768px (tablet md: breakpoint)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<TaskDurationWidget data={data} />);
      });
    });
  });
});
