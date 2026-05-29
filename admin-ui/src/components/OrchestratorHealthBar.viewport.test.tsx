import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OrchestratorHealthBar } from './OrchestratorHealthBar';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
}));

const baseProps = {
  health: {
    last_cycle_seconds_ago: 132,
    // Progress-based liveness: genuinely wedged worst-case (red/down dot with
    // the ⚠ marker) so the widest cycle-indicator rendering is exercised.
    orchestrator_alive: false,
    orchestrator_hung: true,
    orchestrator_progress_age_seconds: 900,
    orchestrator_cycle_age_seconds: 132,
    orchestrator_liveness_reason: 'no progress heartbeat for 900s',
    queued_tasks: 18,
    active_tasks: 7,
    pool_info: {},
    deploy_state: {
      status: 'DEGRADED',
      rollback_count: 2,
      rollback_reason: 'rolling back after a long deployment name exposed a layout edge case',
      pause_reason: 'waiting on a very long maintenance window reason for mobile fit testing',
      monitoring_since: '2026-05-06T23:00:00Z',
      recent_outcomes: [],
    },
    deploy_monitoring: {
      external_health: {
        github_api: 'degraded',
        db: 'ok',
        gcp: 'slow',
      },
    },
  },
  detailedHealth: {
    circuit_breaker: {
      projects: {
        'proj-super-long-name-alpha-beta-gamma-delta-epsilon': { state: 'OPEN', failure_count: 9, opened_at: null },
        'proj-very-long-name-zeta-eta-theta-iota-kappa-lambda': { state: 'OPEN', failure_count: 4, opened_at: null },
      },
    },
    max_plan_pool: { total: 6, available: 0, by_provider: { anthropic: { total: 3, available: 0 } } },
    autonomous: { enabled: true, consecutive_failures: 3 },
    provider_apis: {
      status: 'degraded',
      fallback_enabled: true,
      required_providers: ['anthropic', 'openai', 'gemini'],
      available_providers: ['anthropic'],
      degraded_providers: ['openai'],
      down_providers: ['gemini'],
      providers: {
        anthropic: { provider: 'anthropic', dep_name: 'anthropic', configured: true, dispatch_relevant: true, status: 'ok', reason: null, has_raw_credentials: true, total_accounts: 3, enabled_accounts: 3, ready_accounts: 3, available_accounts: 3, primary_model: 'claude-opus-4-1', dispatch_available: true },
        openai: { provider: 'openai', dep_name: 'openai', configured: true, dispatch_relevant: true, status: 'degraded', reason: 'latency is high enough to keep this pill wide', has_raw_credentials: true, total_accounts: 2, enabled_accounts: 2, ready_accounts: 1, available_accounts: 1, primary_model: 'gpt-5.1', dispatch_available: true },
        gemini: { provider: 'gemini', dep_name: 'gcp', configured: true, dispatch_relevant: true, status: 'down', reason: 'service account credentials missing', has_raw_credentials: false, total_accounts: 1, enabled_accounts: 1, ready_accounts: 0, available_accounts: 0, primary_model: 'gemini-2.5-pro', dispatch_available: false },
      },
    },
    github_quota: {
      remaining: 17,
      limit: 5000,
      reset_at: 1778131200,
      last_updated: '2026-05-06T23:10:00Z',
      status: 'critical',
    },
  },
  apiHealthy: true,
  wsConnected: false,
  lastFetchTime: Date.UTC(2026, 4, 7, 0, 0, 0),
  tasks: [
    { id: 'task-1', status: 'IN_PROGRESS', last_heartbeat: '2026-05-06T23:40:00Z' },
    { id: 'task-2', status: 'IN_PROGRESS', last_heartbeat: '2026-05-06T22:30:00Z' },
    { id: 'task-3', status: 'QUEUED', last_heartbeat: null },
  ],
  pipelineThrottle: {
    reason: 'deploy',
    detail: 'pipeline blocked by a long-running deployment gate reason that should still wrap cleanly',
    recovery_seconds: 98,
  },
};

describe('OrchestratorHealthBar — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) {
      act(() => {
        root?.unmount();
      });
      root = null;
    }
    host?.remove();
    host = null;
  });

  test('worst-case health state has no horizontal overflow at 375px', async () => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ daily_cost_usd: 94.32, daily_cost_ceiling_usd: 100 }),
    } as Response);

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<OrchestratorHealthBar {...baseProps} />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ daily_cost_usd: 94.32, daily_cost_ceiling_usd: 100 }),
    } as Response);

    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<OrchestratorHealthBar {...baseProps} />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
    }, { selector: 'button, a[href]' });
  });

  test('operator-facing text is not silently clipped at 375px', async () => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ daily_cost_usd: 94.32, daily_cost_ceiling_usd: 100 }),
    } as Response);

    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<OrchestratorHealthBar {...baseProps} />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
    });
  });
});
