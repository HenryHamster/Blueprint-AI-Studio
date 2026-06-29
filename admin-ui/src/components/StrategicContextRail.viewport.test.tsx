import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicContextRail } from './StrategicContextRail';
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
});
