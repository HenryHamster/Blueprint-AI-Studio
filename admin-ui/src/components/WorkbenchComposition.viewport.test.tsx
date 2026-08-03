import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { HeaderBar, PrimaryTabNav } from './HeaderBar';
import { MessagesPanel } from './MessagesPanel';
import { StrategicContextRail } from './StrategicContextRail';
import { StrategicSessionControl } from './StrategicSessionControl';
import { WorkbenchFrame, WorkbenchSplit, WorkbenchSurface } from './WorkbenchLayout';
import { expectNoOverflowAt } from '../test/viewport';
import type { StrategicStateResponse } from '../strategicStateApi';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({ messages: [] }), { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
  MESSAGE_CONTENT_MAX: 20000,
  authHeaders: vi.fn(async () => ({ Authorization: 'Bearer test', 'Content-Type': 'application/json' })),
  // MessagesPanel (rendered here) statically imports the pending-confirmation
  // actions; browser-mode ESM requires the mock to expose every named export the
  // subtree imports or the linker fails at import time.
  acceptPendingConfirmation: vi.fn(async () => ({ status: 'accepted' })),
  dismissPendingConfirmation: vi.fn(async () => ({ status: 'dismissed' })),
}));

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

vi.mock('./AutonomousStatus', () => ({
  AutonomousStatus: () => <div data-testid="autonomous-status" />,
}));

vi.mock('./StrategicContextRail', () => ({
  StrategicContextRail: ({
    onOpenRuntimeDevices,
    onOpenRuntimeCompute,
    onOpenAccounts,
    onOpenContextWorkspace,
    onOpenPipeline,
  }: {
    onOpenRuntimeDevices?: () => void;
    onOpenRuntimeCompute?: () => void;
    onOpenAccounts?: () => void;
    onOpenContextWorkspace?: () => void;
    onOpenPipeline?: () => void;
  }) => (
    <div data-testid="strategic-context-rail" className="grid gap-1">
      <button type="button" onClick={onOpenRuntimeDevices}>Devices</button>
      <button type="button" onClick={onOpenAccounts}>Accounts</button>
      <button type="button" onClick={onOpenRuntimeCompute}>Compute</button>
      <button type="button" onClick={onOpenContextWorkspace}>Open context</button>
      <button type="button" onClick={onOpenPipeline}>Pipeline</button>
    </div>
  ),
}));

const noop = () => {};

const STATE = {
  project_id: 'project-123',
  strategic: {
    active_cycle: null,
    active_task: null,
    active_phase: null,
    latest_cycle: {
      id: 'cycle-1',
      outcome: 'completed',
      phase1_task_id: null,
      phase2_task_id: null,
      phase1_started_at: '2026-06-29T01:00:00Z',
      phase1_completed_at: '2026-06-29T01:05:00Z',
      phase2_started_at: '2026-06-29T01:05:00Z',
      phase2_completed_at: '2026-06-29T01:10:00Z',
      summary_text: 'Updated the operator workbench context.',
      failure_reason: null,
      directions_created: 1,
      directions_retired: 0,
      tasks_spawned: 2,
      vision_updated: true,
      problem_model_changed: true,
      running_update_changed: true,
    },
    refresh: { pending: null, last_fired_at: null, last_fired_origin: null },
  },
  running_update: {
    excerpt: 'Strategic is focused on a compact chat surface with durable context.',
    char_count: 72,
    acknowledged_at: null,
  },
  problem_model: {
    revision: 4,
    current_focus: 'Keep the operator workbench chat-first without losing context.',
    active_gap_count: 1,
    capability_count: 4,
    pending_confirmation_count: 0,
    top_gap: {
      id: 'gap-1',
      name: 'Workbench shell composition needs viewport regression coverage.',
      priority: 'high',
      status: 'active',
    },
  },
  directions: {
    total_count: 2,
    active_count: 1,
    paused_count: 0,
    primary: {
      id: 'direction-1',
      text: 'Preserve the real operator workbench shell composition.',
      priority: 'high',
      status: 'active',
      mode: 'fundamentals',
    },
  },
  messages: {
    counts: { open: 1, linked: 1 },
    items: [],
  },
  tasks: {
    counts: {
      queued: 3,
      running: 1,
      evaluating: 0,
      blocked: 0,
      parked: 0,
      failed: 0,
      completed: 12,
      total: 16,
    },
    cumulative_estimated_cost_usd: 3.25,
  },
  attention: {
    level: 'needs_operator',
    summary: '1 item needs operator review',
    last_seen_at: null,
    counts: {
      needs_operator: 1,
      watch: 1,
      visible_needs_operator: 1,
      visible_watch: 1,
      unread: 1,
      questions: 1,
      blockers: 0,
      approvals: 0,
      manual_directions: 0,
      failed_tasks: 0,
      blocked_tasks: 0,
      parked_tasks: 0,
    },
    items: [
      {
        id: 'message:msg-1',
        kind: 'message',
        severity: 'needs_operator',
        reason: 'question',
        label: 'Question waiting',
        ref: { kind: 'message', id: 'msg-1', project_id: 'project-123' },
      },
    ],
  },
  context_package: {
    id: 'strategic-context:project-123',
    title: 'Strategic context',
    status: 'ready',
    item_count: 12,
    token_count: null,
    sources: [
      { kind: 'running_update', count: 1 },
      { kind: 'problem_model', count: 3 },
      { kind: 'tasks', count: 8 },
    ],
    canvas: {
      id: 'strategic-canvas:project-123',
      title: 'Strategic context map',
      kind: 'strategic_context',
      node_count: 4,
      edge_count: 3,
    },
    resources: [
      {
        id: 'strategic-canvas:project-123',
        kind: 'canvas',
        label: 'Strategic context map',
        summary: '4 nodes / 3 links',
        target: { surface: 'project', section: 'problem-model' },
      },
    ],
    drilldowns: [
      { id: 'problem_model', label: 'Model', target: { surface: 'project', section: 'problem-model' } },
      { id: 'pipeline', label: 'Pipeline', target: { surface: 'pipeline' } },
      { id: 'runtime', label: 'Runtime', target: { surface: 'runtime', section: 'health' } },
    ],
  },
  workbench: {
    version: 1,
    interaction_mode: 'continuous',
    active_surface: 'strategic',
    selected_refs: [],
    session_started_at: null,
    last_interaction_at: null,
  },
  runtime: {
    local_runner: {
      mode: 'local_ready',
      strategic_runner_count: 1,
      live_runner_count: 1,
      eligible_live_count: 1,
      stale_runner_count: 0,
      busy_runner_count: 0,
      agent_unavailable_count: 0,
      active_runner_id: null,
      active_task_pinned: false,
      active_task_runtime: null,
      fallback_reason: null,
      liveness_window_seconds: 300,
    },
    work: {
      compute_jobs: {
        window_minutes: 360,
        counts: {
          active: 0,
          terminal_recent: 0,
          total: 1,
          queued: 1,
          starting: 0,
          running: 0,
          polling: 0,
          succeeded_recent: 0,
          failed_recent: 0,
          cancelled_recent: 0,
          timed_out_recent: 0,
          criterion_measured: 0,
          criterion_met: 0,
        },
        spend: {
          active_accrued_usd: 0,
          recent_terminal_usd: 0,
          window_total_usd: 0,
        },
        items: [
          {
            id: 'job-queued',
            task_id: 'task-queued-compute',
            task_title: 'Queued compute check',
            task_type: 'compute_experiment',
            status: 'QUEUED',
            backend: 'local',
            queued_at: '2026-06-29T01:06:00Z',
            cost_usd: 0,
          },
        ],
      },
      held_approvals: { count: 0, items: [] },
      local_runner_holds: { count: 0, items: [] },
      parked_wakes: {
        counts: {
          active: 0,
          pending: 0,
          fired: 0,
          problem: 0,
          expired: 0,
          failed: 0,
        },
        items: [],
      },
    },
  },
  design: { has_design_doc: true, revision: 2 },
} as unknown as StrategicStateResponse;

function makeHeaderProps() {
  return {
    tasks: [],
    onRefresh: noop,
    showResearchBrowser: false,
    onToggleResearchBrowser: noop,
    showKnowledgeBrowser: false,
    onToggleKnowledgeBrowser: noop,
    openMessageCount: 1,
    newSinceLastVisitCount: 1,
    needsAttentionCount: 1,
    onOpenNewTaskModal: noop,
    onOpenSettings: noop,
    canManagePlatform: true,
    onSignOut: noop,
    userEmail: 'operator@example.com',
    wsRef: { current: null },
    apiHealthy: true,
    wsConnected: true,
    lastFetchTime: 0,
    deployState: null,
    selectedProjectId: 'project-123',
    projects: [{ id: 'project-123', name: 'Blueprint', github_owner: 'Blueprint', github_repo: 'Blueprint', status: 'active', autonomous_enabled: false, created_at: '2026-01-01T00:00:00Z' }],
    onSelectProject: noop,
    activeTab: 'chat' as const,
    onTabChange: noop,
    onOpenCommandPalette: vi.fn(),
    health: null,
    detailedHealth: null,
  };
}

async function mockStrategicSession(messages: Array<Record<string, unknown>> = []) {
  const { apiFetch } = await import('../api');
  (apiFetch as ReturnType<typeof vi.fn>).mockImplementation(async (url: unknown, init?: unknown) => {
    const urlStr = String(url);
    const method = (init as RequestInit | undefined)?.method ?? 'GET';
    if (/\/api\/admin\/projects\/[^/]+\/strategic-session$/.test(urlStr) && method === 'GET') {
      return new Response(JSON.stringify({
        conversation: { limit: 20, has_more: false, order: 'oldest_first', items: messages },
        recent_outputs: [],
        session: {},
        status: 'ready',
        location: 'local',
        status_reason: null,
      }), { status: 200 });
    }
    return new Response(JSON.stringify({ messages: [] }), { status: 200 });
  });
}

describe('WorkbenchComposition viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.clearAllMocks();
  });

  async function renderWorkbenchShell(options: {
    onOpenCommandPalette?: () => void;
    onOpenDevices?: () => void;
    onOpenCompute?: () => void;
    onOpenAccounts?: () => void;
    onOpenContext?: () => void;
    onOpenPipeline?: () => void;
  } = {}) {
    const onOpenCommandPalette = options.onOpenCommandPalette ?? vi.fn();
    await mockStrategicSession([
      {
        id: 'msg-1',
        project_id: 'project-123',
        message_type: 'FEEDBACK',
        from_type: 'operator',
        from_task_id: null,
        content: 'Keep the real workbench shell in composition.',
        context: {},
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-06-29T01:00:00Z',
      },
    ]);
    host = document.createElement('div');
    host.style.height = '800px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <div className="flex h-screen min-w-0 flex-col overflow-hidden bg-[#05080c] text-neutral-100">
          <HeaderBar {...makeHeaderProps()} onOpenCommandPalette={onOpenCommandPalette} />
          <WorkbenchFrame>
            <WorkbenchSurface
              title="Strategic"
              showTitle={false}
              actions={(
                <StrategicSessionControl
                  currentMode="chat"
                  sessionActive={false}
                  sessionState="ready"
                  onStartSession={vi.fn()}
                  onStopSession={vi.fn()}
                  onModeChange={vi.fn()}
                  density="compact"
                />
              )}
              flush
            >
              <WorkbenchSplit
                main={(
                  <MessagesPanel
                    canOperateProject={true}
                    selectedProjectId="project-123"
                    addToast={() => {}}
                    onMessageCountChange={() => {}}
                    variant="page"
                    filterChrome="minimal"
                    showCloseButton={false}
                    strategicState={STATE}
                    onOpenCommandPalette={onOpenCommandPalette}
                    onOpenStrategicContextWorkspace={vi.fn()}
                  />
                )}
                rail={(
                  <StrategicContextRail
                    state={STATE}
                    onOpenProjectSection={vi.fn()}
                    onOpenPipeline={options.onOpenPipeline ?? vi.fn()}
                    onOpenRuntimeHealth={vi.fn()}
                    onOpenRuntimeDevices={options.onOpenDevices ?? vi.fn()}
                    onOpenRuntimeCompute={options.onOpenCompute ?? vi.fn()}
                    onOpenAccounts={options.onOpenAccounts ?? vi.fn()}
                    onOpenContextWorkspace={options.onOpenContext ?? vi.fn()}
                    onOpenTask={vi.fn()}
                    onOpenMessages={vi.fn()}
                    onOpenTarget={vi.fn()}
                    accountSpend={{
                      totalAccounts: 2,
                      availableAccounts: 1,
                      enabledAccounts: 1,
                      activeAccountTasks: 1,
                      configuredCapAccounts: 1,
                      disabledModelCount: 0,
                      trackedSpendUsd: 12.34,
                      dailyCostCeilingUsd: 50,
                    }}
                  />
                )}
              />
            </WorkbenchSurface>
          </WorkbenchFrame>
          <PrimaryTabNav
            activeTab="chat"
            onTabChange={vi.fn()}
            openMessageCount={1}
            needsAttentionCount={2}
            placement="mobile"
            className="md:hidden"
          />
        </div>,
      );
    });
    await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 75)); });
  }

  test('desktop shell shows header command search and preserves chat, composer, and rail', async () => {
    await expectNoOverflowAt(1280, renderWorkbenchShell, { height: 800, waitMs: 50 });

    const globalHeader = host?.querySelector('header');
    const headerCommandSearch = globalHeader?.querySelector<HTMLButtonElement>('[data-testid="header-command-search"]');
    const composer = host?.querySelector('[data-testid="strategic-composer-box"]');
    const conversationColumn = host?.querySelector('[data-testid="strategic-conversation-column"]');
    const rail = host?.querySelector('[data-testid="strategic-context-rail"]');

    expect(globalHeader).toBeTruthy();
    expect(headerCommandSearch).toBeTruthy();
    expect(headerCommandSearch?.textContent).toContain('Ctrl K');
    expect(headerCommandSearch?.textContent).not.toContain('Search commands');
    expect(headerCommandSearch?.textContent).not.toContain('Message Strategic');
    expect(composer).toBeTruthy();
    expect(conversationColumn).toBeTruthy();
    expect(rail).toBeTruthy();
    expect(getComputedStyle(rail!.closest('aside')!).display).not.toBe('none');
  });

  test('breakpoint shell keeps monitoring visible with header command launcher', async () => {
    const onOpenCommandPalette = vi.fn();
    const onOpenDevices = vi.fn();
    const onOpenAccounts = vi.fn();
    const onOpenCompute = vi.fn();
    const onOpenContext = vi.fn();
    const onOpenPipeline = vi.fn();

    await expectNoOverflowAt(
      1024,
      () => renderWorkbenchShell({
        onOpenCommandPalette,
        onOpenDevices,
        onOpenAccounts,
        onOpenCompute,
        onOpenContext,
        onOpenPipeline,
      }),
      { height: 800, waitMs: 50 },
    );

    const sessionControl = host?.querySelector('[data-testid="strategic-session-control"]');
    const rail = host?.querySelector('[data-testid="strategic-context-rail"]');
    const railButtons = Array.from(rail?.querySelectorAll<HTMLButtonElement>('button') ?? []);
    const headerCommandSearch = host?.querySelector<HTMLButtonElement>('[data-testid="header-command-search"]');
    const commandButtons = Array.from(host?.querySelectorAll<HTMLButtonElement>('button[title="Open command palette (Ctrl+K)"]') ?? [])
      .filter(button => {
        const rect = button.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && getComputedStyle(button).display !== 'none';
      });
    const composerCommandButtons = commandButtons.filter(button => button.closest('[data-testid="strategic-composer-box"]'));

    expect(sessionControl).toBeTruthy();
    expect(getComputedStyle(sessionControl as Element).display).not.toBe('none');
    expect(sessionControl?.textContent).not.toContain('Mark checked');
    expect(sessionControl?.querySelector('button[aria-label="Switch Strategic mode to Workday"]')).toBeTruthy();
    // check-in is no longer a dedicated session-control button — it moved to the command palette with the mode collapse
    expect(sessionControl?.querySelector('button[aria-label^="Mark Strategic session checked:"]')).toBeNull();
    expect(sessionControl?.querySelector('button[aria-label^="Open devices: runner"]')).toBeNull();
    expect(sessionControl?.querySelector('button[aria-label^="Open accounts:"]')).toBeNull();
    const devicesButton = railButtons.find(button => button.textContent?.trim() === 'Devices');
    const accountsButton = railButtons.find(button => button.textContent?.trim() === 'Accounts');
    const computeButton = railButtons.find(button => button.textContent?.trim() === 'Compute');
    const contextRailButton = railButtons.find(button => button.textContent?.trim() === 'Open context');
    const pipelineButton = railButtons.find(button => button.textContent?.trim() === 'Pipeline');
    expect(accountsButton).toBeTruthy();
    expect(devicesButton).toBeTruthy();
    expect(computeButton).toBeTruthy();
    expect(contextRailButton).toBeTruthy();
    expect(pipelineButton).toBeTruthy();
    expect(rail).toBeTruthy();
    expect(getComputedStyle(rail!.closest('aside')!).display).not.toBe('none');
    expect(host?.querySelector<HTMLButtonElement>('button[aria-label="Open Strategic context"]')).toBeNull();
    expect(headerCommandSearch).toBeTruthy();
    expect(commandButtons).toContain(headerCommandSearch);
    expect(composerCommandButtons).toHaveLength(0);
    await act(async () => { headerCommandSearch!.click(); });
    expect(onOpenCommandPalette).toHaveBeenCalledOnce();
    expect(onOpenDevices).not.toHaveBeenCalled();
    await act(async () => { devicesButton!.click(); });
    expect(onOpenDevices).toHaveBeenCalledOnce();
    await act(async () => { accountsButton!.click(); });
    expect(onOpenAccounts).toHaveBeenCalledOnce();
    await act(async () => { computeButton!.click(); });
    expect(onOpenCompute).toHaveBeenCalledOnce();
    await act(async () => { contextRailButton!.click(); });
    expect(onOpenContext).toHaveBeenCalledOnce();
    await act(async () => { pipelineButton!.click(); });
    expect(onOpenPipeline).toHaveBeenCalledOnce();
  });

  test('mobile shell keeps composer, hides rail, exposes the Workday mode toggle and one context launcher without overflow', async () => {
    await expectNoOverflowAt(375, renderWorkbenchShell, { height: 800, waitMs: 50 });

    const composer = host?.querySelector('[data-testid="strategic-composer-box"]');
    const railAside = host?.querySelector('[data-testid="strategic-context-rail"]')?.closest('aside');
    const contextButtons = Array.from(host?.querySelectorAll<HTMLButtonElement>('button[title="Open Strategic context"]') ?? [])
      .filter(button => {
        const rect = button.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && getComputedStyle(button).display !== 'none';
      });
    const mobileNav = host?.querySelector<HTMLElement>('[data-testid="mobile-primary-tab-nav"]');
    const sessionControl = host?.querySelector('[data-testid="strategic-session-control"]');

    expect(composer).toBeTruthy();
    expect(sessionControl).toBeTruthy();
    // The compact Chat/Workday mode toggle stays visible on mobile; only the
    // detail/session chips collapse (hideDetailsOnMobile), so the operator can
    // switch into Workday from the narrow chat header.
    expect(getComputedStyle(sessionControl as Element).display).not.toBe('none');
    const mobileWorkdayToggle = sessionControl?.querySelector<HTMLButtonElement>('button[aria-label="Switch Strategic mode to Workday"]');
    expect(mobileWorkdayToggle).toBeTruthy();
    expect(getComputedStyle(mobileWorkdayToggle as Element).display).not.toBe('none');
    expect(railAside).toBeTruthy();
    expect(getComputedStyle(railAside!).display).toBe('none');
    expect(contextButtons).toHaveLength(1);
    expect(contextButtons[0].closest('[data-testid="strategic-composer-box"]')).toBeNull();
    expect(contextButtons[0].dataset.testid).toBe('strategic-context-descriptor');
    expect(mobileNav).toBeTruthy();
    expect(getComputedStyle(mobileNav!).display).toBe('grid');
    expect(composer!.getBoundingClientRect().bottom).toBeLessThanOrEqual(mobileNav!.getBoundingClientRect().top);
  });
});
