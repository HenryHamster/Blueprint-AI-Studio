import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, createRef } from 'react';
import { MessagesComposer } from './MessagesComposer';
import { StrategicContextRail } from './StrategicContextRail';
import { WorkbenchSplit, WorkbenchSurface } from './WorkbenchLayout';
import { expectMinTouchTargetAt, expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import type { StrategicStateResponse } from '../strategicStateApi';

const STATE: StrategicStateResponse = {
  project_id: 'proj-1',
  strategic: {
    active_cycle: null,
    active_task: null,
    active_phase: null,
    latest_cycle: {
      id: 'sc-done',
      outcome: 'completed',
      phase1_task_id: null,
      phase2_task_id: null,
      phase1_started_at: '2026-06-29T01:00:00Z',
      phase1_completed_at: '2026-06-29T01:05:00Z',
      phase2_started_at: '2026-06-29T01:05:00Z',
      phase2_completed_at: '2026-06-29T01:10:00Z',
      summary_text: 'Updated the operator workbench direction and running update.',
      failure_reason: null,
      directions_created: 1,
      directions_retired: 0,
      tasks_spawned: 3,
      vision_updated: true,
      problem_model_changed: true,
      running_update_changed: true,
    },
    refresh: {
      pending: {
        origin: 'operator',
        operator_count: 1,
        excerpt: 'Keep the strategic surface simple and persistent.',
      },
      last_fired_at: null,
      last_fired_origin: null,
    },
  },
  running_update: {
    excerpt: 'Strategic is focused on a compact chat surface with durable context and drill-through monitoring.',
    char_count: 96,
    acknowledged_at: null,
  },
  problem_model: {
    revision: 8,
    current_focus: 'Persistent Strategic interaction for active operator work.',
    active_gap_count: 2,
    capability_count: 6,
    pending_confirmation_count: 1,
    top_gap: {
      id: 'g-operator-chat',
      name: 'Operator chat state should survive reloads without extra management UI.',
      priority: 'high',
      status: 'active',
    },
  },
  directions: {
    total_count: 4,
    active_count: 2,
    paused_count: 0,
    primary: {
      id: 'd-workbench',
      text: 'Turn the operator workbench into a clean chat-first strategic surface.',
      priority: 'high',
      status: 'active',
      mode: 'fundamentals',
    },
  },
  messages: { counts: { open: 2, linked: 1 }, items: [] },
  tasks: {
    counts: {
      queued: 5,
      running: 2,
      evaluating: 1,
      blocked: 1,
      parked: 0,
      failed: 0,
      completed: 20,
      total: 29,
    },
    cumulative_estimated_cost_usd: 8.25,
    focus: {
      version: 1,
      bucket_limit: 6,
      has_more: true,
      buckets: {
        needs_judgment: {
          total: 2,
          limit: 6,
          has_more: false,
          items: [
            {
              id: 'task-review-long-focus',
              bucket: 'needs_judgment',
              status: 'EVALUATING',
              title: 'Review whether the Strategic workbench keeps monitoring available without making chat feel crowded',
              task_type: 'review',
              latest_status_message: 'The operator needs a compact drill-through task focus view that does not replace the pipeline.',
              detail: null,
              requires_approval: true,
              reasons: ['approval'],
              direction_id: 'd-workbench',
              created_at: '2026-06-29T01:04:00Z',
            },
            {
              id: 'task-approval-second-focus',
              bucket: 'needs_judgment',
              status: 'PARKED',
              title: 'Confirm whether local Strategic should stay pinned during long workday sessions',
              task_type: 'review',
              latest_status_message: 'A second judgment row keeps the mobile rail dense enough to catch clipping.',
              detail: null,
              requires_approval: true,
              reasons: ['approval', 'parked'],
              direction_id: 'd-workbench',
              created_at: '2026-06-29T01:03:00Z',
            },
          ],
        },
        active: {
          total: 2,
          limit: 6,
          has_more: false,
          items: [
            {
              id: 'task-active-long-focus',
              bucket: 'active',
              status: 'IN_PROGRESS',
              title: 'Polish mounted Strategic rail task focus rows for mobile and desktop',
              task_type: 'fix',
              latest_status_message: 'Rendering long server-authored row text safely in a narrow rail.',
              detail: null,
              requires_approval: false,
              reasons: [],
              created_at: '2026-06-29T01:02:00Z',
            },
            {
              id: 'task-active-second-focus',
              bucket: 'active',
              status: 'PROVISIONING',
              title: 'Prepare command palette routing for the chat first operator shell',
              task_type: 'fix',
              latest_status_message: 'Shortcut coverage is being checked against overlays and text inputs.',
              detail: null,
              requires_approval: false,
              reasons: [],
              created_at: '2026-06-29T01:01:00Z',
            },
          ],
        },
        friction: {
          total: 2,
          limit: 6,
          has_more: false,
          items: [
            {
              id: 'task-friction-long-focus',
              bucket: 'friction',
              status: 'BLOCKED',
              title: 'Blocked self hosted runner registration needs a concrete operator visible status',
              task_type: 'diagnose',
              latest_status_message: 'Device registration should open runtime devices without adding another dashboard.',
              detail: null,
              requires_approval: false,
              reasons: ['blocked'],
              created_at: '2026-06-29T01:00:00Z',
            },
            {
              id: 'task-failed-long-focus',
              bucket: 'friction',
              status: 'FAILED',
              title: 'Failed pipeline extraction review should stay visible without replacing pipeline view',
              task_type: 'fix',
              latest_status_message: 'The task focus block should point to pipeline for deeper inspection.',
              detail: null,
              requires_approval: false,
              reasons: ['failed'],
              created_at: '2026-06-29T00:59:00Z',
            },
          ],
        },
        queued: {
          total: 8,
          limit: 6,
          has_more: true,
          items: [
            {
              id: 'task-queued-long-focus',
              bucket: 'queued',
              status: 'QUEUED',
              title: 'Extract Pipeline workspace after the mounted Strategic rail task focus lands',
              task_type: 'fix',
              latest_status_message: 'Queued work remains inspectable from the pipeline view.',
              detail: null,
              requires_approval: false,
              reasons: [],
              created_at: '2026-06-29T00:58:00Z',
            },
          ],
        },
      },
    },
  },
  attention: {
    level: 'needs_operator',
    summary: '1 operator item needs review',
    last_seen_at: '2026-06-29T01:00:00Z',
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
      failed_tasks: 1,
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
        detail: 'Keep local Strategic enabled?',
        unread: true,
        created_at: '2026-06-29T01:05:00Z',
        ref: { kind: 'message', id: 'msg-1', project_id: 'proj-1' },
      },
    ],
  },
  context_package: {
    id: 'strategic-context:proj-1',
    title: 'Strategic context',
    status: 'ready',
    updated_at: '2026-06-29T01:10:00Z',
    token_count: null,
    item_count: 41,
    sources: [
      { kind: 'running_update', count: 1 },
      { kind: 'problem_model', count: 9 },
      { kind: 'directions', count: 2 },
      { kind: 'tasks', count: 29 },
    ],
    canvas: {
      id: 'strategic-canvas:proj-1',
      title: 'Strategic context map',
      kind: 'strategic_context',
      node_count: 8,
      edge_count: 7,
    },
    resources: [
      {
        id: 'strategic-canvas:proj-1',
        kind: 'canvas',
        label: 'Strategic context map',
        summary: '8 nodes / 7 links',
        target: { surface: 'project', section: 'problem-model' },
      },
      {
        id: 'knowledge:kdoc-1',
        kind: 'knowledge_document',
        label: 'Operator workbench notes',
        summary: 'Context available on request',
        target: { surface: 'knowledge', document_id: 'kdoc-1' },
      },
    ],
    drilldowns: [
      { id: 'running_update', label: 'Update', target: { surface: 'project', section: 'running-update' } },
      { id: 'problem_model', label: 'Model', target: { surface: 'project', section: 'problem-model' } },
      { id: 'pipeline', label: 'Pipeline', target: { surface: 'pipeline' } },
      { id: 'runtime', label: 'Runtime', target: { surface: 'runtime', section: 'health' } },
    ],
    descriptors: [
      {
        schema_version: 1,
        id: 'strategic-canvas:proj-1',
        kind: 'context_map',
        title: 'Strategic context map',
        summary: '8 nodes / 7 links',
        status: 'ready',
        target: { surface: 'project', section: 'problem-model' },
      },
      {
        schema_version: 1,
        id: 'context-metrics',
        kind: 'metric_cards',
        title: 'Context health',
        status: 'ready',
        metrics: [
          { label: 'Items', value: 41, tone: 'info', target: { surface: 'project', section: 'problem-model' } },
          { label: 'Tasks', value: 29, tone: 'neutral', target: { surface: 'pipeline' } },
        ],
      },
    ],
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
  },
  design: { has_design_doc: true, revision: 5 },
};

describe('StrategicContextRail — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  async function mount(width: number) {
    host = document.createElement('div');
    host.style.width = `${width}px`;
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(
        <StrategicContextRail
          state={STATE}
          onOpenProjectSection={vi.fn()}
          onOpenPipeline={vi.fn()}
          onOpenRuntimeHealth={vi.fn()}
          onOpenRuntimeDevices={vi.fn()}
        />,
      );
    });
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => { await mount(375); });
  });

  test('buttons meet touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => { await mount(375); });
  });

  test('important labels are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => { await mount(375); });
  });

  test('renders without horizontal overflow at 1440px — acceptance evidence', async () => {
    await expectNoOverflowAt(1440, async () => { await mount(1440); });
  });

  const STATE_WITH_CONFIRMATIONS: StrategicStateResponse = {
    ...STATE,
    attention: {
      ...STATE.attention!,
      counts: { ...STATE.attention!.counts, needs_operator: 3, pending_confirmations: 2 },
      items: [
        {
          id: 'confirmation:p-2e5edcbc',
          kind: 'confirmation',
          severity: 'needs_operator',
          reason: 'confirmation_credentials_needed',
          confirmation_type: 'credentials_needed',
          label: 'Credentials needed',
          detail: 'Provide the Hugging Face token so the runner can pull the gated model.',
          unread: true,
          created_at: '2026-06-29T01:05:00Z',
          ref: { kind: 'confirmation', id: 'p-2e5edcbc', project_id: 'proj-1' },
        },
        {
          id: 'confirmation:p-0f59f334',
          kind: 'confirmation',
          severity: 'needs_operator',
          reason: 'confirmation_major_decision',
          confirmation_type: 'major_decision',
          label: 'Decision needed',
          detail: 'Should the pipeline adopt the 30-day retention window for execution logs?',
          unread: true,
          created_at: '2026-06-29T01:04:00Z',
          ref: { kind: 'confirmation', id: 'p-0f59f334', project_id: 'proj-1' },
        },
        ...(STATE.attention!.items),
      ],
    },
  };

  async function mountConfirmations(width: number) {
    host = document.createElement('div');
    host.style.width = `${width}px`;
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(
        <StrategicContextRail
          state={STATE_WITH_CONFIRMATIONS}
          onOpenProjectSection={vi.fn()}
          onOpenPipeline={vi.fn()}
          onOpenRuntimeHealth={vi.fn()}
          onOpenRuntimeDevices={vi.fn()}
        />,
      );
    });
  }

  test('needs-your-action confirmation list renders without overflow at 1280px — acceptance evidence', async () => {
    await expectNoOverflowAt(1280, async () => { await mountConfirmations(1280); });
    expect(host?.querySelector('[data-testid="rail-needs-action"]')).toBeTruthy();
    expect(host?.querySelector('[data-testid="rail-needs-action"]')?.textContent).toContain('Needs your action');
  });

  test('needs-your-action confirmation list keeps touch targets and no overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => { await mountConfirmations(375); });
    await expectMinTouchTargetAt(375, async () => { await mountConfirmations(375); });
  });

  async function mountStrategicChatSideboard(onOpenContext = vi.fn()) {
    host = document.createElement('div');
    host.style.height = '800px';
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root!.render(
        <WorkbenchSurface
          title="Strategic"
          showTitle={false}
          actions={(
            <button
              type="button"
              onClick={onOpenContext}
              className="inline-flex min-h-11 items-center rounded-md border border-neutral-800 px-2.5 text-xs font-semibold text-neutral-300 sm:min-h-9 lg:hidden"
              aria-label="Open Strategic context"
            >
              Context
            </button>
          )}
          flush
        >
          <WorkbenchSplit
            main={(
              <div
                className="flex min-h-0 min-w-0 flex-col overflow-hidden bg-[#05080c] text-neutral-200"
                data-testid="strategic-chat-primary"
              >
                <div className="min-h-0 flex-1 p-3">Strategic chat</div>
                <MessagesComposer
                  textareaRef={createRef<HTMLTextAreaElement>()}
                  newType="FEEDBACK"
                  newContent=""
                  submitting={false}
                  replyTo={null}
                  strategistBusy={false}
                  strategicContext={{
                    modeLabel: 'Chat',
                    surfaceLabel: 'Strategic',
                    contextLabel: 'Context ready',
                    contextTone: 'ready',
                    refCount: 1,
                    refs: [{ kind: 'canvas', id: 'canvas:proj-1', label: 'Strategic map' }],
                  }}
                  onChangeType={() => {}}
                  onChangeContent={() => {}}
                  onCancelReply={() => {}}
                  onSubmit={() => {}}
                  chrome="minimal"
                />
              </div>
            )}
            rail={(
              <StrategicContextRail
                state={STATE}
                onOpenProjectSection={vi.fn()}
                onOpenPipeline={vi.fn()}
                onOpenRuntimeHealth={vi.fn()}
                onOpenRuntimeDevices={vi.fn()}
                onOpenRuntimeCompute={vi.fn()}
                onOpenTask={vi.fn()}
                onOpenMessages={vi.fn()}
                onOpenTarget={vi.fn()}
              />
            )}
            railClassName="bg-[#0b1017]"
          />
        </WorkbenchSurface>,
      );
    });
  }

  test('composed Strategic chat sideboard shows the rail only on desktop', async () => {
    await expectNoOverflowAt(1280, async () => { await mountStrategicChatSideboard(); }, { height: 800 });

    const rail = host?.querySelector('aside');
    const contextButton = host?.querySelector<HTMLButtonElement>('button[aria-label="Open Strategic context"]');
    expect(host?.querySelector('[data-testid="strategic-chat-primary"]')?.textContent).toContain('Strategic chat');
    expect(host?.querySelector('[data-testid="strategic-context-descriptor"]')?.tagName).toBe('DIV');
    expect(host?.querySelector('[data-testid="strategic-context-package-summary"]')?.textContent).toContain('Strategic context');
    expect(host?.querySelector('[data-testid="strategic-chat-artifact-slot"]')).toBeNull();
    expect(host?.querySelector('[data-testid="strategic-context-descriptor-context_map"]')).toBeNull();
    expect(rail).toBeTruthy();
    expect(contextButton).toBeTruthy();
    expect(getComputedStyle(rail!).display).not.toBe('none');
    expect(getComputedStyle(contextButton!).display).toBe('none');
  });

  test('composed Strategic chat shell keeps the mobile context drawer button path', async () => {
    const onOpenContext = vi.fn();
    await expectNoOverflowAt(375, async () => { await mountStrategicChatSideboard(onOpenContext); }, { height: 800 });

    const rail = host?.querySelector('aside');
    const contextButton = host?.querySelector<HTMLButtonElement>('button[aria-label="Open Strategic context"]');
    expect(rail).toBeTruthy();
    expect(contextButton).toBeTruthy();
    expect(host?.querySelector('[data-testid="strategic-context-descriptor"]')?.tagName).toBe('DIV');
    expect(host?.querySelector('[data-testid="strategic-chat-artifact-slot"]')).toBeNull();
    expect(getComputedStyle(rail!).display).toBe('none');
    expect(getComputedStyle(contextButton!).display).not.toBe('none');

    await act(async () => { contextButton!.click(); });
    expect(onOpenContext).toHaveBeenCalledTimes(1);
  });
});
