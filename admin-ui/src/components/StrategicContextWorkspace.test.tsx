import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { ComponentProps } from 'react';
import { StrategicContextWorkspace } from './StrategicContextWorkspace';
import type {
  StrategicStateResponse,
  StrategicWorkbenchState,
  StrategicWorkSessionEvent,
} from '../strategicStateApi';

type SessionRefs = StrategicWorkbenchState['selected_refs'];

const STATE = {
  attention: {
    level: 'needs_operator',
    summary: 'One operator decision is waiting.',
    last_seen_at: null,
    counts: {
      needs_operator: 1,
      watch: 2,
      unread: 3,
      questions: 1,
      blockers: 0,
      approvals: 1,
      manual_directions: 0,
      failed_tasks: 1,
      blocked_tasks: 1,
      parked_tasks: 0,
    },
    items: [],
  },
  context_package: {
    id: 'strategic-context:proj-1',
    title: 'Workbench context package',
    status: 'ready',
    updated_at: '2026-06-29T01:00:00Z',
    token_count: 2400,
    item_count: 12,
    sources: [
      { kind: 'running_update', count: 1 },
      { kind: 'tasks', count: 6 },
    ],
    canvas: {
      id: 'canvas:proj-1',
      title: 'Strategic map',
      node_count: 3,
      edge_count: 2,
      nodes: [
        {
          id: 'project',
          kind: 'project',
          label: 'Blueprint',
          summary: 'Operator workbench',
          target: { surface: 'project', section: 'running-update' },
        },
        {
          id: 'problem-model',
          kind: 'project_section',
          label: 'Problem model',
          summary: 'Current product questions',
          target: { surface: 'project', section: 'problem-model' },
        },
        {
          id: 'task-context-1',
          kind: 'task',
          label: 'Investigate local runner',
          summary: 'Runner registration needs validation',
          status: 'blocked',
          count: 2,
          target: { surface: 'task', task_id: 'task-context-1' },
        },
      ],
      edges: [
        { source: 'project', target: 'problem-model', label: 'defines' },
        { source: 'problem-model', target: 'task-context-1', label: 'drives' },
      ],
      drilldowns: [
        { id: 'map', label: 'Problem model', target: { surface: 'project', section: 'problem-model' } },
      ],
    },
    resources: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:operator-notes',
        label: 'Operator notes',
        summary: 'Persistent operator context',
        target: { surface: 'knowledge', document_id: 'operator-notes' },
      },
      {
        kind: 'artifact',
        id: 'artifact:deep-artifact',
        label: 'Deep artifact',
        summary: 'Full workspace should render this row',
        target: { surface: 'task', task_id: 'task-context-1', artifact_id: 'deep-artifact' },
      },
    ],
    evidence: {
      knowledge: {
        total: 1,
        items: [
          {
            id: 'operator-context-brief',
            kind: 'knowledge_document',
            label: 'Operator context brief',
            summary: 'Evidence from project notes',
            target: { surface: 'knowledge', document_id: 'operator-context-brief' },
          },
        ],
      },
      artifacts: {
        total: 1,
        items: [
          {
            id: 'latest-pr',
            kind: 'pull_request',
            label: 'Latest pull request',
            artifact_type: 'pull_request',
            task_title: 'Validate workspace',
            target: { surface: 'task', task_id: 'task-context-2', artifact_id: 'latest-pr' },
          },
        ],
      },
    },
    drilldowns: [
      { id: 'runtime', label: 'Runtime compute', target: { surface: 'runtime', section: 'compute' } },
    ],
  },
  workbench: {
    version: 1,
    interaction_mode: 'continuous',
    active_surface: 'strategic',
    selected_refs: [
      { kind: 'knowledge_document', id: 'knowledge:operator-notes', label: 'Operator notes' },
    ],
  },
  work_session: {
    version: 1,
    id: 'strategic-session:proj-1',
    mode: 'continuous',
    active_surface: 'strategic',
    selected_refs: [
      { kind: 'knowledge_document', id: 'knowledge:operator-notes', label: 'Operator notes' },
    ],
    selected_ref_descriptors: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:operator-notes',
        label: 'Operator notes',
        summary: 'Persistent operator context',
        status: 'reviewing',
        target: { surface: 'knowledge', document_id: 'operator-notes' },
      },
    ],
    curated_metrics: [
      {
        id: 'messages.open',
        label: 'open messages',
        value: 0,
        tone: 'neutral',
        target: { surface: 'messages' },
      },
      {
        id: 'runtime.compute_failed',
        label: 'failed compute',
        value: 2,
        tone: 'critical',
        target: { surface: 'runtime', section: 'compute' },
      },
      {
        id: 'messages.questions',
        label: 'questions',
        value: 1,
        tone: 'warning',
        target: { surface: 'messages' },
      },
    ],
    actions: [
      {
        id: 'reply-operator',
        kind: 'reply',
        label: 'Reply to operator question',
        reason: 'Strategic needs a routing choice',
        priority: 'critical',
        source: 'message',
        target: { surface: 'messages', message_id: 'msg-operator' },
        enabled: true,
      },
      {
        id: 'inspect-compute',
        kind: 'inspect',
        label: 'Inspect compute failures',
        reason: 'Recent compute failed',
        priority: 'high',
        source: 'runtime',
        target: { surface: 'runtime', section: 'compute' },
        enabled: true,
      },
    ],
  },
  runtime: {
    local_runner: {
      mode: 'local_ready',
      strategic_runner_count: 2,
      live_runner_count: 1,
      eligible_live_count: 1,
      stale_runner_count: 0,
      busy_runner_count: 0,
      agent_unavailable_count: 0,
      active_task_pinned: false,
      fallback_reason: null,
      liveness_window_seconds: 90,
    },
    work: {
      compute_jobs: {
        window_minutes: 60,
        counts: {
          active: 1,
          terminal_recent: 3,
          total: 4,
          queued: 2,
          starting: 0,
          running: 1,
          polling: 0,
          succeeded_recent: 2,
          failed_recent: 1,
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
        items: [],
      },
      held_approvals: { count: 1, items: [] },
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
  tasks: {
    counts: {
      queued: 2,
      running: 1,
      evaluating: 0,
      blocked: 1,
      parked: 0,
      failed: 1,
      completed: 3,
      total: 8,
    },
    cumulative_estimated_cost_usd: 0,
  },
} as StrategicStateResponse;

describe('StrategicContextWorkspace', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  function mount(overrides: Partial<ComponentProps<typeof StrategicContextWorkspace>> = {}) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    const props = {
      open: true,
      state: STATE,
      onClose: vi.fn(),
      onOpenTarget: vi.fn(),
      ...overrides,
    };
    act(() => {
      currentRoot.render(<StrategicContextWorkspace {...props} />);
    });
    return { host, props };
  }

  function stateWithWorkSessionEvents(events: StrategicWorkSessionEvent[]): StrategicStateResponse {
    const workSession = STATE.work_session;
    if (!workSession) throw new Error('STATE fixture must include work_session');
    return {
      ...STATE,
      work_session: {
        ...workSession,
        events: {
          version: 1,
          limit: 24,
          has_more: true,
          items: events,
        },
      },
    };
  }

  test('does not render while closed', () => {
    const { host } = mount({ open: false });
    expect(host.textContent).toBe('');
  });

  test('shows a neutral empty state when no context package is loaded', () => {
    const { host } = mount({ state: null });

    expect(host.textContent).toContain('empty');
    expect(host.textContent).toContain('No Strategic context package is loaded for this project.');
    expect(host.textContent).not.toContain('ready');
  });

  test('renders the full Strategic context package', () => {
    const { host } = mount();

    expect(host.textContent).toContain('Strategic context');
    expect(host.textContent).toContain('Session');
    expect(host.querySelector('[data-testid="strategic-session-control"]')).not.toBeNull();
    expect(host.textContent).toContain('Workday');
    expect(host.textContent).toContain('Runner');
    expect(host.textContent).toContain('1 runner ready');
    expect(host.textContent).toContain('Compute');
    expect(host.textContent).toContain('needs check');
    expect(host.textContent).toContain('One operator decision is waiting.');
    expect(host.textContent).toContain('failed compute');
    expect(host.textContent).not.toContain('open messages');
    expect(host.textContent).toContain('Reply to operator question');
    expect(host.textContent).toContain('Workbench context package');
    expect(host.textContent).toContain('Operator notes');
    expect(host.textContent).toContain('reviewing');
    expect(host.textContent).toContain('Problem model');
    expect(host.textContent).toContain('Investigate local runner');
    expect(host.textContent).toContain('blocked');
    expect(host.textContent).toContain('Blueprint');
    expect(host.textContent).toContain('defines');
    expect(host.textContent).toContain('drives');
    expect(host.textContent).toContain('Deep artifact');
    expect(host.textContent).toContain('Operator context brief');
    expect(host.textContent).toContain('Latest pull request');
    expect(host.textContent).toContain('running update 1');
  });

  test('exposes Strategic mode, check-in, and account controls from the drawer', () => {
    const onClose = vi.fn();
    const onModeChange = vi.fn();
    const onCheckIn = vi.fn();
    const onOpenAccounts = vi.fn();
    const { host } = mount({
      onClose,
      onModeChange,
      onCheckIn,
      onOpenAccounts,
      accountSpend: {
        totalAccounts: 3,
        availableAccounts: 2,
        enabledAccounts: 3,
        cooldownAccounts: 0,
        trackedSpendUsd: 12.34,
      },
    });

    const control = host.querySelector('[data-testid="strategic-session-control"]');
    expect(control?.textContent).toContain('Workday');
    expect(control?.textContent).toContain('Mark checked');
    expect(control?.textContent).toContain('Accounts');

    const buttons = Array.from(host.querySelectorAll('button'));
    expect(buttons.filter(button => button.title === 'Open Runner')).toHaveLength(1);
    expect(buttons.filter(button => button.title === 'Open Compute')).toHaveLength(1);
    expect(buttons.some(button => button.getAttribute('aria-label')?.startsWith('Open devices: runner'))).toBe(false);
    expect(buttons.some(button => button.getAttribute('aria-label')?.startsWith('Open compute: runtime work'))).toBe(false);

    buttons
      .find(button => button.getAttribute('aria-label') === 'Switch Strategic mode to Monitor')
      ?.click();
    expect(onModeChange).toHaveBeenCalledWith('monitor');

    buttons
      .find(button => button.getAttribute('aria-label')?.startsWith('Mark Strategic session checked:'))
      ?.click();
    expect(onCheckIn).toHaveBeenCalledTimes(1);

    buttons
      .find(button => button.getAttribute('aria-label')?.startsWith('Open accounts: accounts'))
      ?.click();
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onOpenAccounts).toHaveBeenCalledTimes(1);
  });

  test('keeps canvas resources with the rendered map instead of duplicating them in resources', () => {
    const onUpdateSessionRefs = vi.fn();
    const state = {
      ...STATE,
      context_package: {
        ...STATE.context_package,
        resources: [
          {
            kind: 'canvas',
            id: 'canvas:proj-1',
            label: 'Strategic map resource',
            summary: 'The map is the shared frame for this discussion.',
          },
          ...STATE.context_package.resources,
        ],
      },
    } as StrategicStateResponse;

    const { host } = mount({ state, onUpdateSessionRefs });
    const mapSection = host.querySelector('[data-testid="strategic-context-workspace-map"]');
    const mapResource = host.querySelector('[data-testid="strategic-context-workspace-map-resource"]');
    const resourcesSection = host.querySelector('[data-testid="strategic-context-workspace-resources"]');

    expect(mapSection?.textContent).toContain('Strategic map resource');
    expect(mapResource?.textContent).toContain('The map is the shared frame for this discussion.');
    expect(resourcesSection?.textContent).not.toContain('Strategic map resource');
    expect(Array.from(mapResource?.querySelectorAll('button') ?? []).some(button => button.disabled)).toBe(false);

    Array.from(mapResource?.querySelectorAll('button') ?? [])
      .find(button => button.title === 'Keep Strategic map resource in session context')
      ?.click();

    expect(onUpdateSessionRefs).toHaveBeenCalledWith(expect.any(Function));
    const updater = onUpdateSessionRefs.mock.calls.at(-1)?.[0] as (refs: SessionRefs) => SessionRefs;
    expect(updater([])).toEqual([
      { kind: 'canvas', id: 'canvas:proj-1', label: 'Strategic map resource' },
    ]);
  });

  test('renders parallel map edges in the fallback list without duplicate key warnings', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const state = {
      ...STATE,
      context_package: {
        ...STATE.context_package,
        canvas: {
          ...STATE.context_package.canvas,
          edge_count: 2,
          edges: [
            { source: 'project', target: 'problem-model', label: 'defines' },
            { source: 'project', target: 'problem-model', label: 'defines' },
          ],
        },
      },
    } as StrategicStateResponse;

    const { host } = mount({ state });

    expect(host.querySelector('[data-testid="strategic-context-workspace-edges"]')?.textContent).toContain('defines');
    expect(consoleError).not.toHaveBeenCalledWith(expect.stringContaining('Encountered two children with the same key'));
    consoleError.mockRestore();
  });

  test('keeps and removes Strategic session references through the shared updater', () => {
    const onUpdateSessionRefs = vi.fn();
    const { host } = mount({ onUpdateSessionRefs });
    const buttons = Array.from(host.querySelectorAll('button'));

    buttons.find(button => button.title === 'Keep Deep artifact in session context')?.click();
    expect(onUpdateSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    let updater = onUpdateSessionRefs.mock.calls.at(-1)?.[0] as (refs: SessionRefs) => SessionRefs;
    expect(updater([])).toEqual([
      { kind: 'artifact', id: 'artifact:deep-artifact', label: 'Deep artifact' },
    ]);

    buttons.find(button => button.title === 'Remove Operator notes from session context')?.click();
    expect(onUpdateSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    updater = onUpdateSessionRefs.mock.calls.at(-1)?.[0] as (refs: SessionRefs) => SessionRefs;
    expect(updater([{ kind: 'knowledge_document', id: 'knowledge:operator-notes', label: 'Operator notes' }])).toEqual([]);
  });

  test('opens existing workbench targets and closes the workspace', () => {
    const onClose = vi.fn();
    const onOpenTarget = vi.fn();
    const { host } = mount({ onClose, onOpenTarget });
    const openDeepArtifact = Array.from(host.querySelectorAll('button'))
      .find(button => button.title === 'Open Deep artifact');

    openDeepArtifact?.click();

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onOpenTarget).toHaveBeenCalledWith({
      surface: 'task',
      task_id: 'task-context-1',
      artifact_id: 'deep-artifact',
    });
  });

  test('opens sideboard session targets and closes the workspace', () => {
    const onClose = vi.fn();
    const onOpenTarget = vi.fn();
    const onOpenMessageTarget = vi.fn();
    const { host } = mount({ onClose, onOpenTarget, onOpenMessageTarget });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.title === 'Open Runner')
      ?.click();
    expect(onOpenTarget).toHaveBeenLastCalledWith({ surface: 'runtime', section: 'devices' });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.title === 'Open failed compute')
      ?.click();
    expect(onOpenTarget).toHaveBeenLastCalledWith({ surface: 'runtime', section: 'compute' });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.title === 'Open Reply to operator question')
      ?.click();
    expect(onOpenMessageTarget).toHaveBeenLastCalledWith({ messageId: 'msg-operator', action: 'reply' });
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  test('renders recent session flow and opens existing targets', () => {
    const onClose = vi.fn();
    const onOpenTarget = vi.fn();
    const { host } = mount({
      onClose,
      onOpenTarget,
      state: stateWithWorkSessionEvents([
        {
          id: 'event-runtime',
          schema_version: 1,
          project_id: 'proj-1',
          resource: 'alert',
          action: 'raised',
          refetch: { surfaces: ['runtime'] },
          occurred_at: '2026-06-29T01:02:00Z',
          source: 'runner',
          title: 'Runner stale',
          summary: 'The local runner stopped heartbeating.',
          tone: 'warning',
          target: { surface: 'runtime', section: 'devices' },
        },
      ]),
    });

    expect(host.querySelector('[data-testid="strategic-context-workspace-events"]')?.textContent).toContain('Recent flow');
    expect(host.textContent).toContain('Runner stale');
    expect(host.textContent).toContain('The local runner stopped heartbeating.');

    Array.from(host.querySelectorAll('button'))
      .find(button => button.title === 'Open Runner stale')
      ?.click();

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onOpenTarget).toHaveBeenCalledWith({ surface: 'runtime', section: 'devices' });
  });
});
