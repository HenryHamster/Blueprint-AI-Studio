import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicContextWorkspace } from './StrategicContextWorkspace';
import { expectMinTouchTargetAt, expectNoOverflowAt } from '../test/viewport';
import type { StrategicStateResponse } from '../strategicStateApi';

const STATE = {
  attention: {
    level: 'needs_operator',
    summary: 'A long-running operator attention summary should truncate without widening the sideboard.',
    last_seen_at: null,
    counts: {
      needs_operator: 2,
      watch: 3,
      unread: 4,
      questions: 1,
      blockers: 0,
      approvals: 1,
      manual_directions: 0,
      failed_tasks: 1,
      blocked_tasks: 1,
      parked_tasks: 0,
    },
    items: [{
      id: 'confirmation:viewport-credential',
      kind: 'confirmation',
      label: 'Provide a deployment credential',
      detail: 'Provide a deployment credential before the next release can continue.',
      confirmation_type: 'credentials_needed',
      ref: { id: 'pending-viewport-credential', project_id: 'proj-viewport' },
    }],
  },
  context_package: {
    id: 'strategic-context:proj-viewport',
    title: 'Strategic context package with long operator-facing labels',
    status: 'ready',
    item_count: 18,
    token_count: 3800,
    sources: [
      { kind: 'running_update', count: 1 },
      { kind: 'problem_model', count: 7 },
      { kind: 'tasks', count: 10 },
    ],
    canvas: {
      id: 'canvas:proj-viewport',
      title: 'Strategic context map',
      node_count: 3,
      edge_count: 2,
      nodes: [
        {
          id: 'task-runner-registration',
          kind: 'task',
          label: 'Validate local Strategic runner registration and device liveness',
          summary: 'The operator needs to see whether the connected device can accept Strategic work.',
          status: 'blocked',
          target: { surface: 'task', task_id: 'task-runner-registration' },
        },
        {
          id: 'project-problem-model',
          kind: 'project_section',
          label: 'Problem model and operating assumptions',
          summary: 'Active questions for the workday session.',
          target: { surface: 'project', section: 'problem-model' },
        },
      ],
      edges: [
        { source: 'project-problem-model', target: 'task-runner-registration', label: 'drives' },
      ],
    },
    resources: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:operator-checkin-notes',
        label: 'Operator check-in notes and persistent Strategic context',
        summary: 'Daily notes, decisions, and constraints.',
        target: { surface: 'knowledge', document_id: 'operator-checkin-notes' },
      },
      {
        kind: 'artifact',
        id: 'artifact:runner-registration-report',
        label: 'Runner registration report with diagnostic output',
        summary: 'Self-hosted runner readiness evidence.',
        target: { surface: 'task', task_id: 'task-runner-registration', artifact_id: 'runner-registration-report' },
      },
    ],
    evidence: {
      knowledge: { total: 0, items: [] },
      artifacts: { total: 0, items: [] },
    },
    drilldowns: [
      { id: 'runtime', label: 'Runtime devices', target: { surface: 'runtime', section: 'devices' } },
      { id: 'pipeline', label: 'Pipeline flow', target: { surface: 'pipeline' } },
    ],
  },
  work_session: {
    version: 1,
    id: 'strategic-session:proj-viewport',
    mode: 'continuous',
    active_surface: 'strategic',
    selected_refs: [
      { kind: 'knowledge_document', id: 'knowledge:operator-checkin-notes', label: 'Operator check-in notes and persistent Strategic context' },
    ],
    selected_ref_descriptors: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:operator-checkin-notes',
        label: 'Operator check-in notes and persistent Strategic context',
        summary: 'Daily notes, decisions, and constraints.',
        status: 'active',
        target: { surface: 'knowledge', document_id: 'operator-checkin-notes' },
      },
    ],
    curated_metrics: [],
    actions: [
      {
        id: 'viewport-action',
        kind: 'inspect',
        label: 'Inspect unusually long runtime compute and local runner monitoring target',
        reason: 'The label and detail should stay inside the drawer at narrow widths.',
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
          terminal_recent: 2,
          total: 3,
          queued: 1,
          starting: 0,
          running: 1,
          polling: 0,
          succeeded_recent: 1,
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
  tasks: {
    counts: {
      queued: 1,
      running: 1,
      evaluating: 0,
      blocked: 1,
      parked: 0,
      failed: 1,
      completed: 2,
      total: 6,
    },
    cumulative_estimated_cost_usd: 0,
  },
  strategic_memory_notes: [
    { id: 'mem-1', title: 'Do not assert timelines without supporting evidence — keep projections grounded in measured data.', topic: 'process_rule', summary: 'Keep projections grounded in measured data.', tags: [], created_at: '2026-07-08T14:30:00Z' },
    { id: 'mem-2', title: 'Verify claims from external sources before escalating them to strategic decisions.', topic: 'process_rule', summary: 'Verify external claims before escalating.', tags: [], created_at: '2026-07-08T15:00:00Z' },
    { id: 'mem-3', title: 'Prefer concrete action proposals over abstract discussion when blocking on operator input.', topic: 'operator_preference', summary: 'Prefer concrete action proposals.', tags: [], created_at: '2026-07-09T09:15:00Z' },
  ],
} as unknown as StrategicStateResponse;

describe('StrategicContextWorkspace — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  function mount() {
    if (root) {
      act(() => { root?.unmount(); });
      root = null;
    }
    host?.remove();
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    act(() => {
      currentRoot.render(
        <StrategicContextWorkspace
          open
          state={STATE}
          projectId="proj-viewport"
          canOperateProject
          showAttention={false}
          onClose={vi.fn()}
          onOpenTarget={vi.fn()}
          onUpdateSessionRefs={vi.fn()}
        />,
      );
    });
  }

  test('drawer renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, mount);
  });

  test('drawer controls meet touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, mount);
  });

  test('mobile drawer exposes neutral pending confirmation actions at 375px', async () => {
    await expectNoOverflowAt(375, mount);
    const section = host?.querySelector('[data-testid="strategic-context-workspace-pending-confirmations"]');
    expect(section?.textContent).toContain('Pending confirmations');
    expect(section?.querySelector('[title="Mark provided"]')).toBeTruthy();
    expect(section?.querySelector('[title="Mark as resolved without action"]')).toBeTruthy();
    expect(host?.textContent).not.toContain('Attention');
  });
});
