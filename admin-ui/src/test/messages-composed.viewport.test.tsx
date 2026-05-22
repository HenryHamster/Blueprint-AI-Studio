import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OrchestratorHealthBar } from '../components/OrchestratorHealthBar';
import { ProjectDashboard } from '../components/ProjectDashboard';
import { MessagesPanel } from '../components/MessagesPanel';
import { expectNoOverflowAt } from './viewport';
import type { Project } from '../types/project';

// Unified api mock: OrchestratorHealthBar hits /api/admin/digest, MessagesPanel
// hits /api/admin/messages. Everything else returns {} to avoid null-deref crashes.
vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');

  const digestPayload = {
    daily_cost_usd: 42.0,
    daily_cost_ceiling_usd: 100,
    window_hours: 24,
    generated_at: '2026-05-06T00:00:00Z',
    completed_count: 3,
    failed_count: 0,
    pending_approval_count: 0,
    research_cycles: 1,
    prs_opened: 2,
    active_count: 1,
    running_count: 1,
    needs_attention: [],
    recent_failures: [],
    failure_groups: [],
    recent_completions: [],
    direction_progress: [],
    active_tasks: [],
    fresh_reviewer_effectiveness: null,
  };

  const messagesPayload = {
    messages: [
      {
        id: 'msg-1',
        project_id: 'proj-1',
        message_type: 'NOTE',
        from_type: 'agent',
        from_task_id: 'task-1',
        content: 'Viewport regression signal is working correctly at 375px mobile width',
        context: {},
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-05-06T10:00:00Z',
      },
    ],
  };

  return {
    ...actual,
    API_BASE_URL: 'http://localhost:8000',
    apiFetch: vi.fn(async (url: string) => {
      if (url.includes('/api/admin/digest')) {
        return new Response(JSON.stringify(digestPayload), { status: 200 });
      }
      if (url.includes('/api/admin/messages')) {
        return new Response(JSON.stringify(messagesPayload), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    }),
    authGet: vi.fn(async () => new Response('{}', { status: 200 })),
    authHeaders: vi.fn(async () => ({ Authorization: 'Bearer test', 'Content-Type': 'application/json' })),
  };
});

// MessagesPanel reads auth.currentUser for per-user prefs keys. The mock must
// expose every name firebase.ts exports or browser-mode ESM validation fails.
vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

const healthBarProps = {
  health: {
    last_cycle_seconds_ago: 90,
    queued_tasks: 3,
    active_tasks: 1,
    pool_info: {},
    deploy_state: {
      status: 'OK',
      rollback_count: 0,
      rollback_reason: null,
      pause_reason: null,
      monitoring_since: null,
      recent_outcomes: [],
    },
    deploy_monitoring: null,
  },
  detailedHealth: null,
  apiHealthy: true,
  wsConnected: true,
  lastFetchTime: Date.UTC(2026, 4, 7, 0, 0, 0),
  tasks: [],
  pipelineThrottle: null,
};

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'proj-1',
    name: 'Platform',
    status: 'active',
    github_owner: 'acme',
    github_repo: 'platform',
    autonomous_enabled: true,
    directions: [],
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

describe('MessagesPanel composed with shell panels — viewport regression', () => {
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

  test('OrchestratorHealthBar + ProjectDashboard + MessagesPanel compose without horizontal overflow at 375px', async () => {
    localStorage.setItem('projectDashboardCollapsed', 'false');

    const projects = [
      makeProject({ id: 'p1', name: 'Platform', status: 'active' }),
      makeProject({ id: 'p2', name: 'Mobile App', status: 'paused', autonomous_enabled: false }),
    ];

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.className = 'flex flex-col bg-neutral-950 text-neutral-200 p-3 min-h-screen';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <>
            <div data-testid="header-bar-stub" />
            <OrchestratorHealthBar {...healthBarProps} />
            <ProjectDashboard
              projects={projects}
              tasks={[]}
              selectedProjectId="p1"
              onSelectProject={() => {}}
            />
            <MessagesPanel
              selectedProjectId="p1"
              githubRepo="acme/platform"
              onClose={() => {}}
              addToast={() => {}}
              onMessageCountChange={() => {}}
              onOpenTask={() => {}}
            />
          </>,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
    });

    // Confirm MessagesPanel rendered real content (not an ErrorBoundary fallback).
    // NOTEs appear as a collapsed count ("1 notes") rather than full content,
    // so assert the count chip that proves messages were fetched and displayed.
    expect(host!.textContent).toContain('1 notes');

    localStorage.removeItem('projectDashboardCollapsed');
  });
});
