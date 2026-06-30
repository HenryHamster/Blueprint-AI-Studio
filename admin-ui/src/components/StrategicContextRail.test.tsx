import { afterEach, describe, expect, test, vi } from 'vitest';
import type { ComponentProps } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicContextRail } from './StrategicContextRail';
import type { StrategicStateResponse, StrategicWorkbenchState, StrategicWorkSessionAction } from '../strategicStateApi';

type SessionRefs = StrategicWorkbenchState['selected_refs'];

const STATE: StrategicStateResponse = {
  project_id: 'proj-1',
  strategic: {
    active_cycle: {
      id: 'sc-active',
      outcome: 'phase2_running',
      has_active_task: true,
      stale: false,
      phase1_task_id: 'task-arch',
      phase2_task_id: 'task-strat',
      phase1_started_at: '2026-06-29T01:00:00Z',
      phase1_completed_at: '2026-06-29T01:05:00Z',
      phase2_started_at: '2026-06-29T01:05:00Z',
      phase2_completed_at: null,
      summary_text: null,
      failure_reason: null,
      directions_created: 0,
      directions_retired: 0,
      tasks_spawned: 0,
      vision_updated: false,
      problem_model_changed: false,
      running_update_changed: false,
    },
    active_task: {
      id: 'task-strat',
      status: 'IN_PROGRESS',
      title: 'Strategic review',
      task_type: 'strategic_review',
      latest_status_message: 'Reviewing the workbench',
      started_at: '2026-06-29T01:05:00Z',
      created_at: '2026-06-29T01:04:00Z',
    },
    active_phase: 'strategic_review',
    latest_cycle: {
      id: 'sc-done',
      outcome: 'completed',
      phase1_task_id: 'task-old-1',
      phase2_task_id: 'task-old-2',
      phase1_started_at: '2026-06-28T01:00:00Z',
      phase1_completed_at: '2026-06-28T01:05:00Z',
      phase2_started_at: '2026-06-28T01:05:00Z',
      phase2_completed_at: '2026-06-28T01:10:00Z',
      summary_text: 'Opened the operator workbench direction.',
      failure_reason: null,
      directions_created: 1,
      directions_retired: 0,
      tasks_spawned: 2,
      vision_updated: true,
      problem_model_changed: true,
      running_update_changed: true,
    },
    refresh: {
      pending: {
        origin: 'operator',
        operator_count: 2,
        excerpt: 'Keep the Strategic surface simple.',
      },
      last_fired_at: null,
      last_fired_origin: null,
    },
  },
  running_update: {
    excerpt: 'Strategic is consolidating operator chat state.',
    char_count: 52,
    acknowledged_at: null,
  },
  problem_model: {
    revision: 7,
    current_focus: 'Persistent Strategic interaction',
    purpose: 'Run the pipeline from one workbench.',
    active_gap_count: 3,
    capability_count: 5,
    pending_confirmation_count: 1,
    top_gap: {
      id: 'g-1',
      name: 'Chat reload loses state',
      priority: 'high',
      status: 'active',
    },
  },
  directions: {
    total_count: 4,
    active_count: 2,
    paused_count: 1,
    primary: {
      id: 'd-1',
      text: 'Unblock the operator workbench',
      priority: 'high',
      status: 'active',
      mode: 'fundamentals',
    },
  },
  messages: {
    counts: { open: 1, linked: 2 },
    items: [],
  },
  tasks: {
    counts: {
      queued: 4,
      running: 2,
      evaluating: 1,
      blocked: 1,
      parked: 1,
      failed: 0,
      completed: 12,
      total: 20,
    },
    cumulative_estimated_cost_usd: 3.5,
    focus: {
      version: 1,
      bucket_limit: 6,
      has_more: true,
      buckets: {
        needs_judgment: {
          total: 3,
          limit: 6,
          has_more: false,
          items: [
            {
              id: 'task-judgment',
              bucket: 'needs_judgment',
              status: 'EVALUATING',
              title: 'Review backend focus contract',
              task_type: 'review',
              latest_status_message: 'Strategic needs an operator decision before merging.',
              detail: 'Approval requested for the task focus projection.',
              requires_approval: true,
              reasons: ['approval', 'evaluating'],
              direction_id: 'd-1',
              created_at: '2026-06-29T01:02:00Z',
            },
            {
              id: 'task-second-judgment',
              bucket: 'needs_judgment',
              status: 'BLOCKED',
              title: 'Confirm runner registration posture',
              task_type: 'review',
              latest_status_message: 'This second judgment row reaches the visible cap.',
              detail: null,
              requires_approval: true,
              reasons: ['approval', 'blocked'],
              direction_id: 'd-1',
              created_at: '2026-06-29T01:01:00Z',
            },
            {
              id: 'task-active-focus',
              bucket: 'needs_judgment',
              status: 'EVALUATING',
              title: 'Wire focus rows into the Strategic rail',
              task_type: 'fix',
              latest_status_message: 'Hidden high-priority duplicate should not suppress the visible active row.',
              detail: null,
              requires_approval: true,
              reasons: ['approval'],
              direction_id: 'd-1',
              created_at: '2026-06-29T01:00:00Z',
            },
          ],
        },
        active: {
          total: 3,
          limit: 6,
          has_more: false,
          items: [
            {
              id: 'task-judgment',
              bucket: 'active',
              status: 'EVALUATING',
              title: 'Review backend focus contract',
              task_type: 'review',
              latest_status_message: 'Duplicate active preview should stay under judgment.',
              detail: null,
              requires_approval: true,
              reasons: ['evaluating'],
              direction_id: 'd-1',
              created_at: '2026-06-29T01:02:00Z',
            },
            {
              id: 'task-active-focus',
              bucket: 'active',
              status: 'IN_PROGRESS',
              title: 'Wire focus rows into the Strategic rail',
              task_type: 'fix',
              latest_status_message: 'Rendering server-authored task focus rows.',
              detail: null,
              requires_approval: false,
              reasons: [],
              direction_id: 'd-1',
              started_at: '2026-06-29T01:06:00Z',
              created_at: '2026-06-29T01:04:00Z',
            },
          ],
        },
        friction: {
          total: 1,
          limit: 6,
          has_more: false,
          items: [
            {
              id: 'task-blocked-focus',
              bucket: 'friction',
              status: 'BLOCKED',
              title: 'Blocked runner registration check',
              task_type: 'diagnose',
              latest_status_message: 'Self-hosted runner status needs inspection.',
              detail: 'Runner registration is blocked.',
              requires_approval: false,
              reasons: ['blocked'],
              blocker_kind: 'external',
              blocked_reason: 'Runner not connected.',
              created_at: '2026-06-29T01:01:00Z',
            },
          ],
        },
        queued: {
          total: 7,
          limit: 6,
          has_more: true,
          items: [
            {
              id: 'task-queued-focus',
              bucket: 'queued',
              status: 'QUEUED',
              title: 'Extract Pipeline workspace',
              task_type: 'fix',
              latest_status_message: null,
              detail: 'Ready after the Strategic rail slice lands.',
              requires_approval: false,
              reasons: [],
              created_at: '2026-06-29T00:59:00Z',
            },
          ],
        },
        recent_completed: {
          total: 3,
          limit: 6,
          has_more: false,
          items: [
            {
              id: 'task-completed-focus',
              bucket: 'recent_completed',
              status: 'COMPLETED',
              title: 'Add backend task focus projection',
              task_type: 'create',
              latest_status_message: 'Projection tests passed.',
              detail: null,
              requires_approval: false,
              reasons: [],
              completed_at: '2026-06-29T01:03:00Z',
              created_at: '2026-06-29T00:50:00Z',
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
        detail: 'Keep local Strategic enabled during the workday?',
        unread: true,
        created_at: '2026-06-29T01:05:00Z',
        ref: { kind: 'message', id: 'msg-1', project_id: 'proj-1' },
      },
    ],
  },
  context_package: {
    id: 'strategic-context:proj-1',
    title: 'Workbench context package',
    status: 'building',
    updated_at: '2026-06-29T01:05:00Z',
    token_count: null,
    item_count: 32,
    sources: [
      { kind: 'running_update', count: 1 },
      { kind: 'problem_model', count: 9 },
      { kind: 'directions', count: 2 },
      { kind: 'messages', count: 3 },
      { kind: 'tasks', count: 20 },
    ],
    canvas: {
      id: 'strategic-canvas:proj-1',
      title: 'Strategic context map',
      kind: 'strategic_context',
      node_count: 9,
      edge_count: 8,
      nodes: [
        { id: 'project:proj-1', kind: 'project', label: 'Workbench context package', target: { surface: 'project', section: 'running-update' } },
        { id: 'knowledge:kdoc-1', kind: 'knowledge_document', label: 'Operator notes', target: { surface: 'knowledge', document_id: 'kdoc-1', direction_id: 'd-1', task_id: 'task-strat' } },
        { id: 'artifact:artifact-1', kind: 'artifact', label: 'operator-context-report', target: { surface: 'task', task_id: 'task-strat', artifact_id: 'artifact-1' } },
      ],
      edges: [
        { source: 'project:proj-1', target: 'knowledge:kdoc-1', label: 'knowledge' },
        { source: 'pipeline', target: 'artifact:artifact-1', label: 'artifact' },
      ],
    },
    resources: [
      {
        id: 'strategic-canvas:proj-1',
        kind: 'canvas',
        label: 'Strategic context map',
        summary: '9 nodes / 8 links',
        target: { surface: 'project', section: 'problem-model' },
      },
      {
        id: 'knowledge:kdoc-1',
        kind: 'knowledge_document',
        label: 'Operator notes',
        summary: 'Context should be available when requested.',
        target: { surface: 'knowledge', document_id: 'kdoc-1', direction_id: 'd-1', task_id: 'task-strat' },
      },
      {
        id: 'artifact:artifact-1',
        kind: 'artifact',
        label: 'operator-context-report',
        summary: 'Strategic review',
        target: { surface: 'task', task_id: 'task-strat', artifact_id: 'artifact-1' },
      },
    ],
    evidence: {
      knowledge: {
        total: 1,
        items: [
          {
            id: 'kdoc-1',
            kind: 'knowledge_document',
            label: 'Operator notes',
            summary: 'Context should be available when requested.',
            source_kind: 'design',
            direction_id: 'd-1',
            source_task_id: 'task-strat',
            target: { surface: 'knowledge', document_id: 'kdoc-1', direction_id: 'd-1', task_id: 'task-strat' },
          },
        ],
      },
      artifacts: {
        total: 1,
        items: [
          {
            id: 'artifact-1',
            kind: 'storage',
            label: 'operator-context-report',
            artifact_type: 'INVESTIGATION_REPORT',
            task_id: 'task-strat',
            task_title: 'Strategic review',
            target: { surface: 'task', task_id: 'task-strat', artifact_id: 'artifact-1' },
          },
        ],
      },
    },
    drilldowns: [
      { id: 'running_update', label: 'Update', target: { surface: 'project', section: 'running-update' } },
      { id: 'problem_model', label: 'Model', target: { surface: 'project', section: 'problem-model' } },
      { id: 'pipeline', label: 'Pipeline', target: { surface: 'pipeline' } },
      { id: 'runtime', label: 'Runtime', target: { surface: 'runtime', section: 'health' } },
    ],
  },
  runtime: {
    local_runner: {
      mode: 'local',
      strategic_runner_count: 1,
      live_runner_count: 1,
      eligible_live_count: 1,
      stale_runner_count: 0,
      busy_runner_count: 0,
      agent_unavailable_count: 0,
      active_runner_id: '11111111-1111-1111-1111-111111111111',
      active_task_pinned: true,
      active_task_runtime: {
        provider: 'openai',
        model: 'gpt-5.4',
        account_id: 'acct-local',
        vm_instance_id: 'runner:11111111-1111-1111-1111-111111111111',
        runner_target_id: '11111111-1111-1111-1111-111111111111',
      },
      fallback_reason: null,
      liveness_window_seconds: 300,
    },
    work: {
      compute_jobs: {
        window_minutes: 360,
        counts: {
          active: 2,
          terminal_recent: 1,
          total: 3,
          queued: 0,
          starting: 1,
          running: 1,
          polling: 0,
          succeeded_recent: 0,
          failed_recent: 1,
          cancelled_recent: 0,
          timed_out_recent: 0,
          criterion_measured: 1,
          criterion_met: 0,
        },
        spend: {
          active_accrued_usd: 1.25,
          recent_terminal_usd: 2.5,
          window_total_usd: 3.75,
        },
        items: [
          {
            id: 'job-1',
            task_id: 'task-compute',
            task_title: 'Run GPU benchmark',
            task_type: 'compute_experiment',
            status: 'RUNNING',
            backend: 'local',
            machine_type: 'gpu-small',
            accelerator_type: 'L4',
            accelerator_count: 1,
            cost_usd: 1.25,
            updated_at: '2026-06-29T01:06:00Z',
          },
        ],
      },
      held_approvals: {
        count: 1,
        items: [
          {
            id: 'task-held',
            status: 'QUEUED',
            title: 'Approve expensive benchmark',
            task_type: 'compute_experiment',
            latest_status_message: null,
            started_at: null,
            created_at: '2026-06-29T01:01:00Z',
          },
        ],
      },
      parked_wakes: {
        counts: {
          active: 1,
          pending: 1,
          fired: 0,
          problem: 0,
          expired: 0,
          failed: 0,
        },
        items: [
          {
            id: 'wake-1',
            task_id: 'task-parked',
            task_title: 'Waiting for compute result',
            task_status: 'PARKED',
            wake_source_kind: 'compute_job_terminal',
            wake_source_ref: 'job-1',
            wake_action: 'finalize',
            delivery_state: 'PENDING',
            requested_at: '2026-06-29T01:02:00Z',
            deadline_at: '2026-06-29T02:02:00Z',
            attempt_count: 0,
          },
        ],
      },
    },
  },
  workbench: {
    version: 1,
    interaction_mode: 'continuous',
    active_surface: 'strategic',
    selected_refs: [
      { kind: 'knowledge_document', id: 'knowledge:kdoc-1', label: 'Operator notes' },
    ],
    session_started_at: '2026-06-29T01:00:00Z',
    last_interaction_at: '2026-06-29T01:08:00Z',
  },
  work_session: {
    version: 1,
    id: 'strategic-session:proj-1:2026-06-29T01:00:00Z',
    mode: 'continuous',
    active_surface: 'strategic',
    session_started_at: '2026-06-29T01:00:00Z',
    last_interaction_at: '2026-06-29T01:08:00Z',
    context_package_id: 'strategic-context:proj-1',
    attention_level: 'needs_operator',
    selected_refs: [
      { kind: 'knowledge_document', id: 'knowledge:kdoc-1', label: 'Operator notes' },
    ],
    selected_ref_descriptors: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:kdoc-1',
        label: 'Operator notes',
        summary: 'Context should be available when requested.',
        target: { surface: 'knowledge', document_id: 'kdoc-1', direction_id: 'd-1', task_id: 'task-strat' },
      },
    ],
    curated_metrics: [
      { id: 'attention.needs_operator', label: 'needs operator', value: 1, tone: 'warning', target: { surface: 'messages' } },
      { id: 'tasks.running', label: 'running tasks', value: 2, tone: 'active', target: { surface: 'pipeline' } },
    ],
    context_sources: [
      { kind: 'running_update', count: 1 },
      { kind: 'tasks', count: 20 },
    ],
  },
  design: {
    has_design_doc: true,
    revision: 4,
  },
};

describe('StrategicContextRail', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  function mount(overrides: Partial<ComponentProps<typeof StrategicContextRail>> = {}) {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    const props = {
      state: STATE,
      onOpenProjectSection: vi.fn(),
      onOpenPipeline: vi.fn(),
      onOpenRuntimeHealth: vi.fn(),
      onOpenRuntimeDevices: vi.fn(),
      ...overrides,
    };
    act(() => {
      root!.render(<StrategicContextRail {...props} />);
    });
    return { host, props };
  }

  function stateWithWorkSessionActions(actions: StrategicWorkSessionAction[]): StrategicStateResponse {
    const workSession = STATE.work_session;
    if (!workSession) throw new Error('STATE fixture must include work_session');
    return {
      ...STATE,
      work_session: {
        ...workSession,
        actions,
      },
    };
  }

  test('renders compact persistent Strategic context', () => {
    const { host } = mount({ strategistActive: true, cycleProgress: { task_id: 'task-strat', phase_label: 'Strategic review' } });

    expect(host.textContent).toContain('Strategic review');
    expect(host.textContent).toContain('Strategic review');
    expect(host.textContent).toContain('Persistent Strategic interaction');
    expect(host.textContent).toContain('Chat reload loses state');
    expect(host.textContent).toContain('Unblock the operator workbench');
    expect(host.textContent).toContain('Strategic is consolidating operator chat state');
    expect(host.textContent).toContain('$3.50');
    expect(host.textContent).toContain('Workbench context package');
    expect(host.textContent).toContain('building');
    expect(host.textContent).toContain('32 items');
    expect(host.textContent).toContain('9 nodes / 8 links');
    expect(host.textContent).toContain('Session context');
    expect(host.textContent).toContain('Context map');
    expect(host.textContent).toContain('Strategic context map');
    expect(host.textContent).toContain('Operator notes');
    expect(host.textContent).toContain('operator-context-report');
    expect(host.textContent).toContain('problem model 9');
    expect(host.textContent).toContain('Needs operator');
    expect(host.textContent).toContain('Question waiting');
    expect(host.textContent).toContain('new');
    expect(host.textContent).toContain('linked messages');
    expect(host.textContent).toContain('Task focus');
    expect(host.textContent).toContain('Needs judgment');
    expect(host.textContent).toContain('Review backend focus contract');
    expect(host.textContent).toContain('Wire focus rows into the Strategic rail');
    expect(host.textContent).toContain('Extract Pipeline workspace');
    expect(host.textContent?.match(/Review backend focus contract/g) ?? []).toHaveLength(1);
    expect(host.textContent).toContain('Local Strategic');
    expect(host.textContent).toContain('Running on runner 11111111');
    expect(host.textContent).toContain('gpt-5.4');
    expect(host.textContent).toContain('Runtime work');
    expect(host.textContent).toContain('2 compute jobs');
    expect(host.textContent).toContain('1 approval');
    expect(host.textContent).toContain('Run GPU benchmark');
    expect(host.textContent).toContain('parked');
  });

  test('drills into existing workbench sections', () => {
    const onOpenRuntimeCompute = vi.fn();
    const onOpenKnowledge = vi.fn();
    const onOpenTask = vi.fn();
    const { host, props } = mount({ onOpenRuntimeCompute, onOpenKnowledge, onOpenTask });

    const buttons = Array.from(host.querySelectorAll('button'));
    buttons.find(button => button.textContent?.includes('Strategic context map'))?.click();
    buttons.find(button => button.textContent?.includes('Operator notes'))?.click();
    buttons.find(button => button.textContent?.includes('operator-context-report'))?.click();
    buttons.find(button => button.textContent?.includes('Model'))?.click();
    buttons.find(button => button.textContent?.includes('Directions'))?.click();
    buttons.find(button => button.textContent?.trim() === 'Pipeline')?.click();
    buttons.find(button => button.textContent?.includes('Runtime'))?.click();
    buttons.find(button => button.textContent?.includes('Devices'))?.click();
    buttons.find(button => button.textContent?.includes('Compute'))?.click();
    buttons.find(button => button.textContent?.includes('Review backend focus contract'))?.click();

    expect(props.onOpenProjectSection).toHaveBeenCalledWith('problem-model');
    expect(onOpenKnowledge).toHaveBeenCalledWith('kdoc-1', 'd-1');
    expect(onOpenTask).toHaveBeenCalledWith('task-strat');
    expect(onOpenTask).toHaveBeenCalledWith('task-judgment');
    expect(props.onOpenProjectSection).toHaveBeenCalledWith('directions');
    expect(props.onOpenPipeline).toHaveBeenCalled();
    expect(props.onOpenRuntimeHealth).toHaveBeenCalled();
    expect(props.onOpenRuntimeDevices).toHaveBeenCalled();
    expect(onOpenRuntimeCompute).toHaveBeenCalled();
  });

  test('delegates context package targets to the canonical handler when provided', () => {
    const onOpenTarget = vi.fn();
    const { host, props } = mount({ onOpenTarget });

    const buttons = Array.from(host.querySelectorAll('button'));
    buttons.find(button => button.textContent?.includes('Strategic context map'))?.click();
    buttons.find(button => button.textContent?.includes('Operator notes'))?.click();
    buttons.find(button => button.textContent?.includes('operator-context-report'))?.click();

    expect(onOpenTarget).toHaveBeenCalledWith({ surface: 'project', section: 'problem-model' });
    expect(onOpenTarget).toHaveBeenCalledWith({
      surface: 'knowledge',
      document_id: 'kdoc-1',
      direction_id: 'd-1',
      task_id: 'task-strat',
    });
    expect(onOpenTarget).toHaveBeenCalledWith({
      surface: 'task',
      task_id: 'task-strat',
      artifact_id: 'artifact-1',
    });
    expect(props.onOpenProjectSection).not.toHaveBeenCalled();
    expect(props.onOpenPipeline).not.toHaveBeenCalled();
  });

  test('does not delegate context resources without targets', () => {
    const contextPackage = STATE.context_package;
    if (!contextPackage) throw new Error('Expected fixture context package');
    const onOpenTarget = vi.fn();
    const { host, props } = mount({
      onOpenTarget,
      state: {
        ...STATE,
        context_package: {
          ...contextPackage,
          canvas: contextPackage.canvas ? { ...contextPackage.canvas, nodes: [] } : undefined,
          resources: [
            {
              id: 'artifact-without-target',
              kind: 'artifact',
              label: 'Unrouted context report',
              summary: 'No target yet',
            },
          ],
        },
      },
    });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Unrouted context report'))
      ?.click();

    expect(onOpenTarget).not.toHaveBeenCalled();
    expect(props.onOpenPipeline).not.toHaveBeenCalled();
    expect(props.onOpenProjectSection).not.toHaveBeenCalled();
  });

  test('applies Strategic session ref updates against the latest workbench state', () => {
    const baseWorkbench = STATE.workbench;
    if (!baseWorkbench) throw new Error('Expected fixture workbench state');
    let currentRefs: SessionRefs = [...baseWorkbench.selected_refs];
    const updates: SessionRefs[] = [];
    const onUpdateSessionRefs = vi.fn((updateRefs: (refs: SessionRefs) => SessionRefs) => {
      currentRefs = updateRefs(currentRefs);
      updates.push(currentRefs);
    });
    const firstMount = mount({ onUpdateSessionRefs });

    let buttons = Array.from(firstMount.host.querySelectorAll('button'));
    const keepCanvas = buttons.find(button => button.title === 'Keep Strategic context map in session context');
    const keepArtifact = buttons.find(button => button.title === 'Keep operator-context-report in session context');
    if (!keepCanvas || !keepArtifact) throw new Error('Expected keep actions for context refs');
    expect(keepCanvas.getAttribute('aria-label')).toBe('Keep Strategic context map in session context');
    keepCanvas.click();
    keepArtifact.click();

    expect(updates).toEqual([
      [
        { kind: 'canvas', id: 'strategic-canvas:proj-1', label: 'Strategic context map' },
        { kind: 'knowledge_document', id: 'knowledge:kdoc-1', label: 'Operator notes' },
      ],
      [
        { kind: 'artifact', id: 'artifact:artifact-1', label: 'operator-context-report' },
        { kind: 'canvas', id: 'strategic-canvas:proj-1', label: 'Strategic context map' },
        { kind: 'knowledge_document', id: 'knowledge:kdoc-1', label: 'Operator notes' },
      ],
    ]);
    expect(onUpdateSessionRefs).toHaveBeenCalledTimes(2);

    act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;

    const withSelectedRefs = {
      ...baseWorkbench,
      selected_refs: currentRefs,
    };
    const mounted = mount({ onUpdateSessionRefs, workbenchState: withSelectedRefs });

    expect(mounted.host.textContent).toContain('Strategic context map');
    buttons = Array.from(mounted.host.querySelectorAll('button'));
    const removeCanvas = buttons.find(button => button.title === 'Remove Strategic context map from session context');
    const removeKnowledge = buttons.find(button => button.title === 'Remove Operator notes from session context');
    if (!removeCanvas || !removeKnowledge) throw new Error('Expected remove actions for selected refs');
    expect(removeCanvas.getAttribute('aria-label')).toBe('Remove Strategic context map from session context');
    removeCanvas.click();
    removeKnowledge.click();

    expect(updates.at(-1)).toEqual([
      { kind: 'artifact', id: 'artifact:artifact-1', label: 'operator-context-report' },
    ]);
    expect(onUpdateSessionRefs).toHaveBeenCalledTimes(4);
  });

  test('shows recent terminal compute results without requiring an active job', () => {
    const terminalOnlyState: StrategicStateResponse = {
      ...STATE,
      runtime: {
        ...STATE.runtime,
        work: {
          compute_jobs: {
            window_minutes: 360,
            counts: {
              active: 0,
              terminal_recent: 1,
              total: 1,
              queued: 0,
              starting: 0,
              running: 0,
              polling: 0,
              succeeded_recent: 1,
              failed_recent: 0,
              cancelled_recent: 0,
              timed_out_recent: 0,
              criterion_measured: 1,
              criterion_met: 1,
            },
            spend: {
              active_accrued_usd: 0,
              recent_terminal_usd: 0.75,
              window_total_usd: 0.75,
            },
            items: [
              {
                id: 'job-success',
                task_id: 'task-success',
                task_title: 'Compare embeddings',
                task_type: 'compute_experiment',
                status: 'SUCCEEDED',
                backend: 'local',
                completed_at: '2026-06-29T01:06:00Z',
                cost_usd: 0.75,
              },
            ],
          },
          held_approvals: { count: 0, items: [] },
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
    };
    const { host } = mount({ state: terminalOnlyState });

    expect(host.textContent).toContain('Runtime work');
    expect(host.textContent).toContain('recent result');
    expect(host.textContent).toContain('1 recent compute result');
    expect(host.textContent).toContain('Compare embeddings');
  });

  test('surfaces local runner holds as runtime work needing attention', () => {
    const localRunnerHoldState: StrategicStateResponse = {
      ...STATE,
      runtime: {
        ...STATE.runtime,
        local_runner: null,
        work: {
          compute_jobs: {
            window_minutes: 360,
            counts: {
              active: 0,
              terminal_recent: 0,
              total: 0,
              queued: 0,
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
            items: [],
          },
          held_approvals: { count: 0, items: [] },
          local_runner_holds: {
            count: 1,
            items: [
              {
                id: 'task-local-hold',
                status: 'QUEUED',
                title: 'Run local GPU benchmark',
                task_type: 'compute_experiment',
                latest_status_message: 'Waiting for local runner.',
                started_at: null,
                created_at: '2026-06-29T01:01:00Z',
                awaiting_since: '2026-06-29T01:02:00Z',
              },
            ],
          },
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
    };
    const onOpenRuntimeDevices = vi.fn();
    const onOpenRuntimeCompute = vi.fn();
    const { host } = mount({ state: localRunnerHoldState, onOpenRuntimeDevices, onOpenRuntimeCompute });

    expect(host.textContent).toContain('Runtime work');
    expect(host.textContent).toContain('needs check');
    expect(host.textContent).toContain('1 local runner hold');
    expect(host.textContent).toContain('Run local GPU benchmark');

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Devices'))
      ?.click();

    expect(onOpenRuntimeDevices).toHaveBeenCalled();
    expect(onOpenRuntimeCompute).not.toHaveBeenCalled();
  });

  test('shows local Strategic fallback without adding a second runner list', () => {
    const noRunnerState: StrategicStateResponse = {
      ...STATE,
      runtime: {
        local_runner: {
          ...STATE.runtime!.local_runner!,
          mode: 'cloud',
          strategic_runner_count: 0,
          live_runner_count: 0,
          eligible_live_count: 0,
          stale_runner_count: 0,
          active_runner_id: null,
          active_task_pinned: false,
          active_task_runtime: null,
          fallback_reason: 'no_runner',
        },
      },
    };
    const { host } = mount({ state: noRunnerState });

    expect(host.textContent).toContain('Local Strategic');
    expect(host.textContent).toContain('No local Strategic runner enrolled');
    expect(Array.from(host.querySelectorAll('button')).filter(button => button.textContent?.includes('Devices'))).toHaveLength(1);
  });

  test('does not render a context package block before the backend descriptor exists', () => {
    const { host } = mount({ state: { ...STATE, context_package: null } });

    expect(host.textContent).not.toContain('Workbench context package');
    expect(host.textContent).toContain('Running update');
  });

  test('attention items drill into existing surfaces', () => {
    const attentionState: StrategicStateResponse = {
      ...STATE,
      attention: {
        ...STATE.attention!,
        items: [
          {
            id: 'task:task-1:failed_task',
            kind: 'task',
            severity: 'watch',
            reason: 'failed_task',
            label: 'Task failed',
            detail: 'Viewport test failed.',
            ref: { kind: 'task', id: 'task-1', project_id: 'proj-1' },
          },
          {
            id: 'direction:d-1:manual_intervention',
            kind: 'direction',
            severity: 'needs_operator',
            reason: 'manual_intervention',
            label: 'Direction needs intervention',
            detail: 'Choose the operating mode.',
            ref: { kind: 'direction', id: 'd-1', project_id: 'proj-1' },
          },
        ],
      },
    };
    const { host, props } = mount({ state: attentionState });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Task failed'))
      ?.click();
    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Direction needs intervention'))
      ?.click();

    expect(props.onOpenPipeline).toHaveBeenCalled();
    expect(props.onOpenProjectSection).toHaveBeenCalledWith('directions');
  });

  test('task attention items open the task drawer when task navigation is available', () => {
    const attentionState: StrategicStateResponse = {
      ...STATE,
      attention: {
        ...STATE.attention!,
        items: [
          {
            id: 'task:task-1:failed_task',
            kind: 'task',
            severity: 'watch',
            reason: 'failed_task',
            label: 'Task failed',
            detail: 'Viewport test failed.',
            ref: { kind: 'task', id: 'task-1', project_id: 'proj-1' },
          },
        ],
      },
    };
    const onOpenTask = vi.fn();
    const { host, props } = mount({ state: attentionState, onOpenTask });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Task failed'))
      ?.click();

    expect(onOpenTask).toHaveBeenCalledWith('task-1');
    expect(props.onOpenPipeline).not.toHaveBeenCalled();
  });

  test('message attention item is navigable', () => {
    const attentionState: StrategicStateResponse = {
      ...STATE,
      attention: {
        ...STATE.attention!,
        items: [
          {
            id: 'message:msg-1',
            kind: 'message',
            severity: 'needs_operator',
            reason: 'question',
            label: 'Question waiting',
            detail: 'Keep local Strategic enabled during the workday?',
            unread: true,
            created_at: '2026-06-29T01:05:00Z',
            ref: { kind: 'message', id: 'msg-1', project_id: 'proj-1' },
          },
        ],
      },
    };
    const onOpenMessages = vi.fn();
    const onOpenTarget = vi.fn();
    const { host, props } = mount({ state: attentionState, onOpenMessages, onOpenTarget });

    const btn = Array.from(host.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Question waiting'),
    );
    expect(btn).toBeTruthy();
    btn?.click();

    expect(onOpenMessages).toHaveBeenCalledWith({ messageId: 'msg-1', action: 'reply' });
    expect(onOpenTarget).not.toHaveBeenCalled();
    expect(props.onOpenPipeline).not.toHaveBeenCalled();
  });

  test('renders work session next actions and routes message actions to the message opener', () => {
    const onOpenMessages = vi.fn();
    const onOpenTask = vi.fn();
    const onOpenTarget = vi.fn();
    const { host, props } = mount({
      state: stateWithWorkSessionActions([
        {
          id: 'action-reply',
          kind: 'reply',
          label: 'Reply to operator question',
          reason: 'A high-signal project message is waiting for a direct answer.',
          priority: 'critical',
          source: 'message',
          target: { surface: 'messages', message_id: 'msg-action' },
          enabled: true,
        },
        {
          id: 'action-review-task',
          kind: 'review',
          label: 'Review rollout task',
          reason: 'The active task needs operator judgment.',
          priority: 'high',
          source: 'task',
          target: { surface: 'task', task_id: 'task-action' },
          enabled: true,
        },
        {
          id: 'action-runtime',
          kind: 'inspect',
          label: 'Inspect runtime work',
          reason: 'Runtime work has a recent failure.',
          priority: 'normal',
          source: 'runtime',
          target: { surface: 'runtime', section: 'compute' },
          enabled: true,
        },
        {
          id: 'action-context',
          kind: 'open',
          label: 'Open project model',
          reason: 'Context points at the current problem model.',
          priority: 'normal',
          source: 'context',
          target: { surface: 'project', section: 'problem-model' },
          enabled: true,
        },
        {
          id: 'action-hidden',
          kind: 'open',
          label: 'Hidden fifth action',
          reason: 'Only the top four actions should render.',
          priority: 'low',
          source: 'session',
          target: { surface: 'pipeline' },
          enabled: true,
        },
      ]),
      onOpenMessages,
      onOpenTask,
      onOpenTarget,
    });

    expect(host.textContent).toContain('Next actions');
    expect(host.textContent).toContain('Reply to operator question');
    expect(host.textContent).toContain('Review rollout task');
    expect(host.textContent).toContain('Question waiting');
    expect(host.textContent).not.toContain('Hidden fifth action');

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Reply to operator question'))
      ?.click();

    expect(onOpenMessages).toHaveBeenCalledWith({ messageId: 'msg-action', action: 'reply' });
    expect(onOpenTask).not.toHaveBeenCalled();
    expect(onOpenTarget).not.toHaveBeenCalled();
    expect(props.onOpenPipeline).not.toHaveBeenCalled();
  });

  test('routes work session task actions through task navigation', () => {
    const onOpenTask = vi.fn();
    const onOpenTarget = vi.fn();
    const { host, props } = mount({
      state: stateWithWorkSessionActions([
        {
          id: 'action-review-task',
          kind: 'review',
          label: 'Review rollout task',
          reason: 'The active task needs operator judgment.',
          priority: 'high',
          source: 'task',
          target: { surface: 'task', task_id: 'task-action' },
          enabled: true,
        },
      ]),
      onOpenTask,
      onOpenTarget,
    });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Review rollout task'))
      ?.click();

    expect(onOpenTask).toHaveBeenCalledWith('task-action');
    expect(onOpenTarget).not.toHaveBeenCalled();
    expect(props.onOpenPipeline).not.toHaveBeenCalled();
  });

  test('keeps attention rows when matching work session action is outside the visible action cap', () => {
    const { host } = mount({
      state: stateWithWorkSessionActions([
        {
          id: 'action-one',
          kind: 'open',
          label: 'First action',
          reason: 'Visible non-attention action.',
          priority: 'high',
          source: 'session',
          target: { surface: 'runtime', section: 'health' },
          enabled: true,
        },
        {
          id: 'action-two',
          kind: 'open',
          label: 'Second action',
          reason: 'Visible non-attention action.',
          priority: 'high',
          source: 'session',
          target: { surface: 'runtime', section: 'compute' },
          enabled: true,
        },
        {
          id: 'action-three',
          kind: 'open',
          label: 'Third action',
          reason: 'Visible non-attention action.',
          priority: 'normal',
          source: 'session',
          target: { surface: 'pipeline' },
          enabled: true,
        },
        {
          id: 'action-four',
          kind: 'open',
          label: 'Fourth action',
          reason: 'Visible non-attention action.',
          priority: 'normal',
          source: 'session',
          target: { surface: 'project', section: 'problem-model' },
          enabled: true,
        },
        {
          id: 'attention.message:msg-1',
          kind: 'reply',
          label: 'Hidden duplicate attention action',
          reason: 'This duplicate is outside the rendered action cap.',
          priority: 'low',
          source: 'attention',
          target: { surface: 'messages', message_id: 'msg-1' },
          enabled: true,
        },
      ]),
    });

    expect(host.textContent).toContain('Question waiting');
    expect(host.textContent).not.toContain('Hidden duplicate attention action');
  });

  test('routes work session runtime actions through the canonical target opener', () => {
    const onOpenTarget = vi.fn();
    const onOpenTask = vi.fn();
    const { host, props } = mount({
      state: stateWithWorkSessionActions([
        {
          id: 'action-runtime',
          kind: 'inspect',
          label: 'Inspect runtime work',
          reason: 'Runtime work has a recent failure.',
          priority: 'normal',
          source: 'runtime',
          target: { surface: 'runtime', section: 'compute', task_id: 'task-runtime' },
          enabled: true,
        },
      ]),
      onOpenTarget,
      onOpenTask,
    });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Inspect runtime work'))
      ?.click();

    expect(onOpenTarget).toHaveBeenCalledWith({ surface: 'runtime', section: 'compute', task_id: 'task-runtime' });
    expect(onOpenTask).not.toHaveBeenCalled();
    expect(props.onOpenRuntimeHealth).not.toHaveBeenCalled();
  });

  test('routes work session pipeline actions through the canonical target opener', () => {
    const onOpenTarget = vi.fn();
    const onOpenTask = vi.fn();
    const { host } = mount({
      state: stateWithWorkSessionActions([
        {
          id: 'action-pipeline',
          kind: 'open',
          label: 'Open pipeline flow',
          reason: 'Pipeline flow is the intended target, not direct task navigation.',
          priority: 'normal',
          source: 'session',
          target: { surface: 'pipeline', section: 'flow', task_id: 'task-pipeline' },
          enabled: true,
        },
      ]),
      onOpenTarget,
      onOpenTask,
    });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Open pipeline flow'))
      ?.click();

    expect(onOpenTarget).toHaveBeenCalledWith({ surface: 'pipeline', section: 'flow', task_id: 'task-pipeline' });
    expect(onOpenTask).not.toHaveBeenCalled();
  });

  test('preserves canonical work session target fields for knowledge actions', () => {
    const onOpenTarget = vi.fn();
    const { host } = mount({
      state: stateWithWorkSessionActions([
        {
          id: 'action-knowledge',
          kind: 'open',
          label: 'Open evidence note',
          reason: 'Evidence should retain its document and artifact target fields.',
          priority: 'normal',
          source: 'context',
          target: {
            surface: 'knowledge',
            document_id: 'kdoc-action',
            artifact_id: 'artifact-action',
            direction_id: 'd-action',
          },
          enabled: true,
        },
      ]),
      onOpenTarget,
    });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Open evidence note'))
      ?.click();

    expect(onOpenTarget).toHaveBeenCalledWith({
      surface: 'knowledge',
      document_id: 'kdoc-action',
      artifact_id: 'artifact-action',
      direction_id: 'd-action',
    });
  });

  test('message attention item falls back to pipeline when onOpenMessages is absent', () => {
    const attentionState: StrategicStateResponse = {
      ...STATE,
      attention: {
        ...STATE.attention!,
        items: [
          {
            id: 'message:msg-2',
            kind: 'message',
            severity: 'needs_operator',
            reason: 'blocker',
            label: 'Blocker reported',
            detail: 'CI is broken.',
            ref: { kind: 'message', id: 'msg-2', project_id: 'proj-1' },
          },
        ],
      },
    };
    const { host, props } = mount({ state: attentionState });

    const btn = Array.from(host.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Blocker reported'),
    );
    expect(btn).toBeTruthy();
    btn?.click();

    expect(props.onOpenPipeline).toHaveBeenCalled();
  });

  test('alert attention items drill into runtime health', () => {
    const attentionState: StrategicStateResponse = {
      ...STATE,
      attention: {
        ...STATE.attention!,
        items: [
          {
            id: 'alert:alert-1',
            kind: 'alert',
            severity: 'watch',
            reason: 'alert',
            label: 'Runner offline',
            detail: 'Local runner stopped heartbeating.',
            ref: { kind: 'alert', id: 'alert-1', project_id: 'proj-1' },
          },
        ],
      },
    };
    const { host, props } = mount({ state: attentionState });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Runner offline'))
      ?.click();

    expect(props.onOpenRuntimeHealth).toHaveBeenCalled();
  });

  test('task-linked alert attention item opens task drawer', () => {
    const attentionState: StrategicStateResponse = {
      ...STATE,
      attention: {
        ...STATE.attention!,
        items: [
          {
            id: 'alert:alert-2',
            kind: 'alert',
            severity: 'watch',
            reason: 'alert',
            label: 'Task performance degraded',
            detail: 'Task runtime exceeded expectations.',
            ref: { kind: 'alert', id: 'alert-2', project_id: 'proj-1' },
            task_id: 'task-perf-1',
          },
        ],
      },
    };
    const onOpenTask = vi.fn();
    const { host, props } = mount({ state: attentionState, onOpenTask });

    Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Task performance degraded'))
      ?.click();

    expect(onOpenTask).toHaveBeenCalledWith('task-perf-1');
    expect(props.onOpenRuntimeHealth).not.toHaveBeenCalled();
  });

  test('shows stale instead of working when a persisted cycle has no live task', () => {
    const staleState: StrategicStateResponse = {
      ...STATE,
      strategic: {
        ...STATE.strategic,
        active_cycle: {
          ...STATE.strategic.active_cycle!,
          has_active_task: false,
          stale: true,
        },
        active_task: null,
      },
    };
    const { host } = mount({ state: staleState, strategistActive: true });

    expect(host.textContent).toContain('Cycle stale');
    expect(host.textContent).toContain('no live Strategic task');
    expect(host.textContent).not.toContain('Cycle running');
  });

  test('shows SHA-aware stale label with both candidate and integration SHAs when sha_freshness is stale', () => {
    const staleState: StrategicStateResponse = {
      ...STATE,
      strategic: {
        ...STATE.strategic,
        active_cycle: {
          ...STATE.strategic.active_cycle!,
          has_active_task: false,
          stale: true,
        },
        active_task: null,
      },
      sha_freshness: {
        verdict: 'stale',
        active_candidate_sha: 'abc1234def56',
        integration_head_sha: 'xyz9876uvw01',
      },
    };
    const { host } = mount({ state: staleState, strategistActive: false });

    expect(host.textContent).toContain('Cycle stale');
    expect(host.textContent).toContain('candidate @abc1234');
    expect(host.textContent).toContain('integration @xyz9876');
  });

  test('falls back to generic Cycle stale when sha_freshness verdict is not stale', () => {
    const staleState: StrategicStateResponse = {
      ...STATE,
      strategic: {
        ...STATE.strategic,
        active_cycle: {
          ...STATE.strategic.active_cycle!,
          has_active_task: false,
          stale: true,
        },
        active_task: null,
      },
      sha_freshness: {
        verdict: 'unknown',
        active_candidate_sha: null,
        integration_head_sha: null,
      },
    };
    const { host } = mount({ state: staleState, strategistActive: false });

    expect(host.textContent).toContain('Cycle stale');
    expect(host.textContent).not.toContain('candidate @');
    expect(host.textContent).not.toContain('integration @');
  });

  test('falls back to generic Cycle stale when sha_freshness is absent', () => {
    const staleState: StrategicStateResponse = {
      ...STATE,
      strategic: {
        ...STATE.strategic,
        active_cycle: {
          ...STATE.strategic.active_cycle!,
          has_active_task: false,
          stale: true,
        },
        active_task: null,
      },
    };
    const { host } = mount({ state: staleState, strategistActive: false });

    expect(host.textContent).toContain('Cycle stale');
    expect(host.textContent).not.toContain('candidate @');
    expect(host.textContent).not.toContain('integration @');
  });

  describe('shows_payload_freshness_for_fresh_stale_and_unknown_states', () => {
    test('fresh state shows emerald verdict and candidate SHA', () => {
      const freshState: StrategicStateResponse = {
        ...STATE,
        sha_freshness: {
          verdict: 'fresh',
          active_candidate_sha: 'abc1234def5678',
          integration_head_sha: 'abc1234def5678',
          checked_at: '2026-06-30T10:30:00',
          integration_branch: 'main',
        },
      };
      const { host } = mount({ state: freshState });
      expect(host.textContent).toContain('Payload freshness');
      expect(host.textContent).toContain('fresh');
      expect(host.textContent).toContain('candidate @abc1234');
      expect(host.textContent).toContain('on main');
    });

    test('stale state shows stale verdict, candidate SHA, and integration head SHA', () => {
      const staleState: StrategicStateResponse = {
        ...STATE,
        sha_freshness: {
          verdict: 'stale',
          active_candidate_sha: 'abc1234def5678',
          integration_head_sha: 'xyz9876uvw0123',
          checked_at: '2026-06-30T10:30:00',
          integration_branch: 'main',
        },
      };
      const { host } = mount({ state: staleState });
      expect(host.textContent).toContain('Payload freshness');
      expect(host.textContent).toContain('stale');
      expect(host.textContent).toContain('candidate @abc1234');
      expect(host.textContent).toContain('head @xyz9876');
      expect(host.textContent).toContain('on main');
    });

    test('unknown state shows unknown verdict without SHA fields', () => {
      const unknownState: StrategicStateResponse = {
        ...STATE,
        sha_freshness: {
          verdict: 'unknown',
          checked_at: '2026-06-30T10:30:00',
        },
      };
      const { host } = mount({ state: unknownState });
      expect(host.textContent).toContain('Payload freshness');
      expect(host.textContent).toContain('unknown');
      expect(host.textContent).not.toContain('candidate @');
      expect(host.textContent).not.toContain('head @');
    });

    test('graceful render when sha_freshness is absent from response', () => {
      const noFreshnessState: StrategicStateResponse = {
        ...STATE,
        sha_freshness: undefined,
      };
      const { host } = mount({ state: noFreshnessState });
      expect(host.textContent).not.toContain('Payload freshness');
    });
  });

  test('distinguishes automated refreshes from operator steers', () => {
    const refreshState: StrategicStateResponse = {
      ...STATE,
      strategic: {
        ...STATE.strategic,
        active_cycle: null,
        active_task: null,
        active_phase: null,
        refresh: {
          pending: {
            origin: 'qualification',
            operator_count: 0,
            excerpt: 'Strategic should refresh after qualification changed.',
          },
          last_fired_at: null,
          last_fired_origin: null,
        },
      },
    };
    const { host } = mount({ state: refreshState, strategistActive: false });

    expect(host.textContent).toContain('Refresh queued');
    expect(host.textContent).not.toContain('Steer queued');
  });

  test('keeps clear attention state quiet while preserving counts', () => {
    const clearState: StrategicStateResponse = {
      ...STATE,
      attention: {
        level: 'clear',
        summary: 'No operator action is currently flagged',
        last_seen_at: null,
        counts: {
          needs_operator: 0,
          watch: 0,
          unread: 0,
          questions: 0,
          blockers: 0,
          approvals: 0,
          manual_directions: 0,
          failed_tasks: 0,
          blocked_tasks: 0,
          parked_tasks: 0,
        },
        items: [],
      },
    };
    const { host } = mount({ state: clearState });

    expect(host.textContent).toContain('needs operator');
    expect(host.textContent).not.toContain('Needs operator');
    expect(host.textContent).not.toContain('Question waiting');
  });
});
