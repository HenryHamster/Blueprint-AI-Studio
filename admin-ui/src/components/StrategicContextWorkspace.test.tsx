import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { ComponentProps } from 'react';
import { StrategicContextWorkspace } from './StrategicContextWorkspace';
import { acceptPendingConfirmation, dismissPendingConfirmation } from '../api';
import type {
  StrategicStateResponse,
  StrategicWorkbenchState,
  StrategicWorkSessionEvent,
} from '../strategicStateApi';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    acceptPendingConfirmation: vi.fn(async () => ({ status: 'accepted', type: 'credentials_needed' })),
    dismissPendingConfirmation: vi.fn(async () => {}),
  });
});

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
      starting: 1,
      running: 1,
      evaluating: 2,
      blocked: 1,
      parked: 0,
      failed: 1,
      completed: 3,
      total: 11,
    },
    cumulative_estimated_cost_usd: 0,
  },
} as unknown as StrategicStateResponse;

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

  function stateWithPendingConfirmation(): StrategicStateResponse {
    return {
      ...STATE,
      attention: {
        ...STATE.attention!,
        items: [{
          id: 'confirmation:mobile-credential',
          kind: 'confirmation',
          severity: 'needs_operator',
          reason: 'credentials_needed',
          label: 'Provide the deployment credential',
          detail: 'Provide the deployment credential before the next release.',
          confirmation_type: 'credentials_needed',
          ref: { kind: 'confirmation', id: 'pending-mobile-credential', project_id: 'proj-1' },
        }],
      },
    };
  }

  test('does not render while closed', () => {
    const { host } = mount({ open: false });
    expect(host.textContent).toBe('');
  });

  test('separates starting and evaluating work from in-progress claims', () => {
    const { host } = mount();

    expect(host.textContent).toContain('1 in progress');
    expect(host.textContent).toContain('1 starting / 2 evaluating / 2 queued');
  });

  test('keeps pending confirmation actions reachable without attention chrome', async () => {
    vi.mocked(acceptPendingConfirmation).mockClear();
    vi.mocked(dismissPendingConfirmation).mockClear();
    const onConfirmationResolved = vi.fn();
    const { host } = mount({
      state: stateWithPendingConfirmation(),
      projectId: 'proj-1',
      canOperateProject: true,
      showAttention: false,
      onConfirmationResolved,
    });

    const section = host.querySelector('[data-testid="strategic-context-workspace-pending-confirmations"]');
    expect(section?.textContent).toContain('Pending confirmations');
    expect(host.textContent).not.toContain('Attention');
    expect(host.textContent).not.toContain('needs operator');
    expect(host.textContent).not.toContain('Reply to operator question');
    const acceptButton = Array.from(section!.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.title === 'Mark provided');
    expect(acceptButton).toBeTruthy();

    await act(async () => { acceptButton!.click(); });

    expect(acceptPendingConfirmation).toHaveBeenCalledWith(
      'proj-1',
      'pending-mobile-credential',
      undefined,
    );
    expect(onConfirmationResolved).toHaveBeenCalledOnce();
  });

  test('pending confirmations fail closed without operate permission', () => {
    const { host } = mount({
      state: stateWithPendingConfirmation(),
      projectId: 'proj-1',
      canOperateProject: false,
      showAttention: false,
    });

    const section = host.querySelector('[data-testid="strategic-context-workspace-pending-confirmations"]');
    expect(section?.textContent).toContain('Pending confirmations');
    expect(section?.querySelector('[title="Mark provided"]')).toBeNull();
    expect(section?.querySelector('[title="Mark as resolved without action"]')).toBeNull();
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
    expect(host.textContent).toContain('Runner');
    expect(host.textContent).toContain('1 runner ready');
    expect(host.textContent).toContain('Compute');
    expect(host.textContent).toContain('needs check');
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
    expect(host.querySelector(
      '[data-testid="strategic-context-workspace-artifacts"] [data-testid="strategic-context-descriptor-context_map"]',
    )).toBeNull();
    expect(host.querySelector('[data-testid="strategic-context-workspace-map"]')).not.toBeNull();
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
          ...(STATE.context_package?.resources ?? []),
        ],
      },
    } as unknown as StrategicStateResponse;

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

  test('preserves descriptor-only context map metadata when canvas data is not loaded', () => {
    const state = {
      ...STATE,
      context_package: {
        ...STATE.context_package,
        canvas: null,
        descriptors: [
          {
            id: 'context-map-descriptor',
            kind: 'context_map',
            title: 'Descriptor-only strategic map',
            summary: 'Map is still being prepared.',
            status: 'building',
            target: { surface: 'project', section: 'problem-model' },
          },
        ],
      },
    } as unknown as StrategicStateResponse;

    const { host } = mount({ state });
    const artifactsSection = host.querySelector('[data-testid="strategic-context-workspace-artifacts"]');

    expect(artifactsSection?.textContent).toContain('Descriptor-only strategic map');
    expect(artifactsSection?.textContent).toContain('Map is still being prepared.');
    expect(artifactsSection?.querySelector('[data-testid="strategic-context-descriptor-context_map"]')).not.toBeNull();
  });

  test('renders parallel map edges in the fallback list without duplicate key warnings', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const state = {
      ...STATE,
      context_package: {
        ...STATE.context_package,
        canvas: {
          ...(STATE.context_package?.canvas ?? {}),
          edge_count: 2,
          edges: [
            { source: 'project', target: 'problem-model', label: 'defines' },
            { source: 'project', target: 'problem-model', label: 'defines' },
          ],
        },
      },
    } as unknown as StrategicStateResponse;

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

  test('renders model-integrity conflicts with recovery hints and criterion links', () => {
    const stateWithIntegrity: StrategicStateResponse = {
      ...STATE,
      model_integrity: {
        orphaned_gaps: [
          { id: 'g-abcdef12', name: 'Cross-cycle memory', status: 'active', recovery_hint: 'Assign an active direction to this gap or record a deferral.' },
        ],
        ungrounded_directions: [
          { id: 'd-1', text: 'Refactor auth', gap_id: null, reason: 'no_gap_id', recovery_hint: 'Link this direction to an open gap or retire it.' },
        ],
        stale_active_moves: [
          { gap_id: 'g-2', gap_name: 'Payload budget', stale_direction_ids: ['d-old'], recovery_hint: "Reconcile the gap's active_moves — the listed directions are no longer active." },
        ],
        criterion_link_conflicts: [
          { direction_id: 'd-3', direction_text: 'Ship valve', criterion_key: 'c-dead', classification: 'inactive', recovery_hint: 'Remap to an active criterion — this key is retracted or superseded.' },
          { direction_id: 'd-4', direction_text: 'Wrong version', criterion_key: 'c-ghost', classification: 'dangling', recovery_hint: 'Remap to a criterion in the open version — this key is unknown there.' },
        ],
        counts: {
          orphaned_gaps: 1,
          ungrounded_directions: 1,
          stale_active_moves: 1,
          criterion_link_conflicts: 2,
          total: 5,
        },
      },
    };
    const { host } = mount({ state: stateWithIntegrity });

    const panel = host.querySelector('[data-testid="strategic-context-workspace-integrity"]');
    expect(panel).not.toBeNull();
    const text = panel?.textContent ?? '';
    expect(text).toContain('5 conflicts');
    // Gap-integrity classes render with their recovery hints.
    expect(text).toContain('orphaned gap');
    expect(text).toContain('Assign an active direction to this gap or record a deferral.');
    expect(text).toContain('ungrounded direction');
    expect(text).toContain('Link this direction to an open gap or retire it.');
    expect(text).toContain('stale active_moves');
    // Criterion-mapping classes render distinctly with their own hints.
    expect(text).toContain('inactive criterion link');
    expect(text).toContain('criterion c-dead is retracted or superseded');
    expect(text).toContain('Remap to an active criterion — this key is retracted or superseded.');
    expect(text).toContain('dangling criterion link');
    expect(text).toContain('criterion c-ghost is not in the open version');
    expect(text).toContain('Remap to a criterion in the open version — this key is unknown there.');
  });

  test('hides the model-integrity panel when there are no conflicts', () => {
    const { host } = mount();
    expect(host.querySelector('[data-testid="strategic-context-workspace-integrity"]')).toBeNull();
  });

  test('renders inline viewable thumbnails on an artifact-evidence row when present', () => {
    const contextPackage = STATE.context_package!;
    const artifacts = contextPackage.evidence!.artifacts!;
    const stateWithPreview = {
      ...STATE,
      context_package: {
        ...contextPackage,
        evidence: {
          ...contextPackage.evidence,
          artifacts: {
            ...artifacts,
            items: [
              {
                ...artifacts.items[0],
                viewable_previews: [
                  {
                    url: 'https://storage.example.com/rollout.png?sig=baked',
                    kind: 'image',
                    filename: 'rollout.png',
                    storage_uri: 'gs://bucket/proj/rollout.png',
                  },
                ],
              },
            ],
          },
        },
      },
    } as unknown as StrategicStateResponse;

    // projectId omitted → uses the baked URL directly (no signed-URL fetch).
    const { host } = mount({ state: stateWithPreview });
    const strip = host.querySelector('[data-testid="strategic-artifact-viewable-previews"]');
    expect(strip).not.toBeNull();
    const img = strip?.querySelector('img');
    expect(img).not.toBeNull();
    expect(img!.getAttribute('src')).toContain('sig=baked');
  });

  test('renders no viewable-preview strip on an artifact-evidence row without previews', () => {
    const { host } = mount();
    const artifactsSection = host.querySelector('[data-testid="strategic-context-workspace-evidence"]');
    expect(artifactsSection).not.toBeNull();
    expect(artifactsSection?.querySelector('[data-testid="strategic-artifact-viewable-previews"]')).toBeNull();
  });

  test('evidence section shows durable total count when available items equal total', () => {
    // STATE fixture has total:1 for both knowledge and artifacts, items.length=1 each → total=2.
    const { host } = mount();
    const evidenceSection = host.querySelector('[data-testid="strategic-context-workspace-evidence"]');
    expect(evidenceSection).not.toBeNull();
    // No truncation marker — displayed count equals total.
    expect(evidenceSection?.textContent).not.toContain('showing');
    expect(evidenceSection?.textContent).not.toContain('of');
  });

  test('evidence section shows "showing N of M" when durable total exceeds rendered items', () => {
    const contextPackage = STATE.context_package!;
    const stateWithTruncation = {
      ...STATE,
      context_package: {
        ...contextPackage,
        evidence: {
          knowledge: {
            total: 10,
            pending_count: 0,
            items: contextPackage.evidence!.knowledge!.items,
          },
          artifacts: {
            total: 25,
            items: contextPackage.evidence!.artifacts!.items,
          },
        },
      },
    } as unknown as StrategicStateResponse;

    const { host } = mount({ state: stateWithTruncation });
    const evidenceSection = host.querySelector('[data-testid="strategic-context-workspace-evidence"]');
    expect(evidenceSection).not.toBeNull();
    // knowledge.items.length(1) + artifacts.items.length(1) = 2 displayed; total = 10+25 = 35.
    expect(evidenceSection?.textContent).toContain('showing 2 of 35');
  });

  test('evidence section shows live count alongside total when running jobs present', () => {
    const contextPackage = STATE.context_package!;
    const stateWithLive = {
      ...STATE,
      context_package: {
        ...contextPackage,
        evidence: {
          knowledge: { total: 1, items: contextPackage.evidence!.knowledge!.items },
          artifacts: { total: 3, live_count: 2, items: contextPackage.evidence!.artifacts!.items },
        },
      },
    } as unknown as StrategicStateResponse;

    const { host } = mount({ state: stateWithLive });
    const evidenceSection = host.querySelector('[data-testid="strategic-context-workspace-evidence"]');
    expect(evidenceSection).not.toBeNull();
    expect(evidenceSection?.textContent).toContain('2 live');
  });
});
