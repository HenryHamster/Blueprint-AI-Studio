import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AutonomousStatus } from './AutonomousStatus';
import { expectNoOverflowAt } from '../test/viewport';

// AutonomousStatus uses authHeaders + apiFetch from ../api (transitively imports firebase)
// and calls raw fetch() for the main autonomous-status endpoint.
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('null', { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
  authHeaders: vi.fn(async () => ({ Authorization: 'Bearer test' })),
}));

const PROJECT: Record<string, unknown> = {
  project_id: 'proj-1',
  project_name: 'Blueprint AI Studio',
  autonomous_enabled: true,
  consecutive_failures: 0,
  last_research_at: new Date(Date.now() - 600_000).toISOString(),
  tasks_spawned_last_hour: 2,
  rate_limit: 10,
};

const AUTONOMY_DEBUG = {
  spawn_blocked: true,
  blocking_guards: [
    { guard: 'waiting_for_interval', reason: 'waiting', seconds_until_check: 300,
      next_check_at: new Date(Date.now() + 300_000).toISOString() },
  ],
  circuit_breaker: {
    state: 'CLOSED', failure_count: 0, success_count: 5,
    opened_at: null, cooldown_seconds: 300, cooldown_ends_at: null,
  },
  rate_limit: { tasks_this_hour: 2, max_per_hour: 10 },
  directions: [],
  next_check_at: new Date(Date.now() + 300_000).toISOString(),
};

const STRATEGIC_DEBUG = {
  phase: 0,
  blocking_guards: [],
  has_purpose: true,
  research_spawn_count: 5,
  strategic_cycle_count: 3,
  strategic_frequency: 10,
  architecture_review_frequency: 3,
  next_strategic_in: 5,
  next_is_architecture: false,
  strategic_failures: 0,
  strategic_failures_at: null,
  strategic_failures_decay_at: null,
  architecture: {
    revision: 2,
    last_updated: new Date(Date.now() - 3_600_000).toISOString(),
    updated_by_task: 'task-abc',
    system_count: 5,
    capability_count: 12,
    next_arch_in_cycles: 2,
  },
  active_task: null,
  last_cycle: { outcome: 'success', phase2_completed_at: new Date(Date.now() - 1_800_000).toISOString() },
  recent_cycles: [],
};

describe('AutonomousStatus — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    vi.restoreAllMocks();
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

  test('collapsed control renders without overflow at 1280px (desktop home)', async () => {
    // Desktop first-class placement: AutonomousStatus now mounts in the header at
    // >=768px (previously md:hidden). Capture it at the acceptance viewport width.
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ projects: [PROJECT] }), { status: 200 }),
    ));

    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<AutonomousStatus />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });

  test('collapsed pill renders without horizontal overflow at 375px', async () => {
    // Stub fetch so the background poll on mount doesn't throw.
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ projects: [] }), { status: 200 }),
    ));

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<AutonomousStatus />);
      });
    });
  });

  test('expanded dropdown fits within 375px viewport', async () => {
    // Return one project from the autonomous-status endpoint (raw fetch),
    // plus debug data from the per-project apiFetch calls.
    vi.stubGlobal('fetch', vi.fn(async (url: unknown) => {
      if (typeof url === 'string' && url.includes('autonomous-status')) {
        return new Response(JSON.stringify({ projects: [PROJECT] }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    }));

    const { apiFetch } = await import('../api');
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
      if (url.includes('autonomy-debug')) {
        return new Response(JSON.stringify(AUTONOMY_DEBUG), { status: 200 });
      }
      if (url.includes('strategic-debug')) {
        return new Response(JSON.stringify(STRATEGIC_DEBUG), { status: 200 });
      }
      return new Response('null', { status: 200 });
    });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      await act(async () => {
        r.render(<AutonomousStatus />);
      });

      // Let mount fetches resolve and state update.
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });

      // Click the toggle button to open the dropdown.
      const btn = host.querySelector('button');
      if (btn) {
        await act(async () => { btn.click(); });
      }

      // Let the expanded panel settle.
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });

  test('expanded dropdown with blocking guard renders without overflow at 375px', async () => {
    const blockedDebug = {
      ...AUTONOMY_DEBUG,
      spawn_blocked: true,
      blocking_guards: [
        { guard: 'circuit_breaker_open', reason: 'circuit breaker open',
          failure_count: 3, cooldown_ends_at: new Date(Date.now() + 600_000).toISOString() },
      ],
      circuit_breaker: {
        state: 'OPEN', failure_count: 3, success_count: 0,
        opened_at: new Date(Date.now() - 60_000).toISOString(),
        cooldown_seconds: 600,
        cooldown_ends_at: new Date(Date.now() + 600_000).toISOString(),
      },
    };

    vi.stubGlobal('fetch', vi.fn(async (url: unknown) => {
      if (typeof url === 'string' && url.includes('autonomous-status')) {
        return new Response(JSON.stringify({ projects: [PROJECT] }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    }));

    const { apiFetch } = await import('../api');
    (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: string) => {
      if (url.includes('autonomy-debug')) {
        return new Response(JSON.stringify(blockedDebug), { status: 200 });
      }
      if (url.includes('strategic-debug')) {
        return new Response(JSON.stringify(STRATEGIC_DEBUG), { status: 200 });
      }
      return new Response('null', { status: 200 });
    });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      await act(async () => {
        r.render(<AutonomousStatus />);
      });

      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });

      const btn = host.querySelector('button');
      if (btn) {
        await act(async () => { btn.click(); });
      }

      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    }, { waitMs: 50 });
  });
});
