import { describe, expect, test } from 'vitest';
import type { ProjectMessageContext } from '../types/messages';
import type { StrategicStateResponse } from '../strategicStateApi';
import {
  buildMessageStrategicContextDescriptor,
  buildStrategicContextDescriptor,
} from './strategicContextDescriptor';

function makeStrategicState(overrides: Partial<StrategicStateResponse> = {}): StrategicStateResponse {
  return {
    project_id: 'proj-1',
    strategic: {
      active_cycle: null,
      active_task: null,
      active_phase: null,
      latest_cycle: null,
      refresh: { pending: null, last_fired_at: null, last_fired_origin: null },
    },
    running_update: { excerpt: null, char_count: 0, acknowledged_at: null },
    problem_model: {
      active_gap_count: 0,
      capability_count: 0,
      pending_confirmation_count: 0,
      top_gap: null,
    },
    directions: { total_count: 0, active_count: 0, paused_count: 0, primary: null },
    messages: { counts: { open: 0, linked: 0 }, items: [] },
    tasks: {
      counts: {
        queued: 0,
        running: 0,
        evaluating: 0,
        blocked: 0,
        parked: 0,
        failed: 0,
        completed: 0,
        total: 0,
      },
      cumulative_estimated_cost_usd: 0,
    },
    attention: {
      level: 'clear',
      summary: '',
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
    context_package: null,
    workbench: {
      version: 1,
      interaction_mode: 'chat',
      active_surface: 'strategic',
      selected_refs: [],
      session_started_at: null,
      last_interaction_at: null,
    },
    design: { has_design_doc: false, revision: null },
    ...overrides,
  };
}

describe('strategicContextDescriptor freshness', () => {
  test('uses last_interaction_at freshness over stale session_started_at', () => {
    const recentNaive = new Date(Date.now() - 30 * 60_000).toISOString().replace('Z', '');
    const staleNaive = new Date(Date.now() - 5 * 3600_000).toISOString().replace('Z', '');

    const descriptor = buildStrategicContextDescriptor(
      makeStrategicState({
        workbench: {
          version: 1,
          interaction_mode: 'continuous',
          active_surface: 'strategic',
          session_started_at: staleNaive,
          last_interaction_at: recentNaive,
          selected_refs: [],
        },
      }),
    );

    expect(descriptor).not.toBeNull();
    expect(descriptor?.modeLabel).toBe('Workday');
  });

  test('includes package, canvas, and selected ref descriptor details', () => {
    const recentNaive = new Date(Date.now() - 30 * 60_000).toISOString().replace('Z', '');

    const descriptor = buildStrategicContextDescriptor(
      makeStrategicState({
        context_package: {
          id: 'strategic-context:proj-1',
          title: 'Operator workbench plan',
          status: 'building',
          item_count: 14,
          token_count: null,
          sources: [],
          resources: [],
          drilldowns: [],
          canvas: {
            id: 'strategic-canvas:proj-1',
            node_count: 8,
            edge_count: 7,
            nodes: [],
            edges: [],
          },
        },
        workbench: {
          version: 1,
          interaction_mode: 'continuous',
          active_surface: 'project',
          session_started_at: recentNaive,
          last_interaction_at: recentNaive,
          selected_refs: [
            { kind: 'task', id: 'task-1', label: 'Stale task label' },
          ],
        },
        work_session: {
          version: 1,
          id: 'strategic-session:proj-1',
          mode: 'continuous',
          active_surface: 'project',
          session_started_at: recentNaive,
          last_interaction_at: recentNaive,
          context_package_id: 'strategic-context:proj-1',
          attention_level: 'clear',
          selected_refs: [],
          selected_ref_descriptors: [
            {
              kind: 'task',
              id: 'task-1',
              label: 'Review active wave',
              summary: 'Reviewing context descriptors.',
              status: 'IN_PROGRESS',
              target: { surface: 'task', task_id: 'task-1' },
            },
          ],
          curated_metrics: [],
          context_sources: [],
        },
      }),
    );

    expect(descriptor).not.toBeNull();
    expect(descriptor?.packageTitle).toBe('Operator workbench plan');
    expect(descriptor?.contextLabel).toBe('Building context');
    expect(descriptor?.canvas).toEqual({ nodeCount: 8, edgeCount: 7 });
    expect(descriptor?.itemCount).toBe(14);
    expect(descriptor?.refs[0]).toMatchObject({
      kind: 'task',
      label: 'Review active wave',
      summary: 'Reviewing context descriptors.',
      status: 'IN_PROGRESS',
    });
  });

  test('suppresses stale session-only workbench signal', () => {
    const staleNaive = new Date(Date.now() - 5 * 3600_000).toISOString().replace('Z', '');

    const descriptor = buildStrategicContextDescriptor(
      makeStrategicState({
        work_session: {
          version: 1,
          id: 'strategic-session:proj-1',
          mode: 'continuous',
          active_surface: 'pipeline',
          session_started_at: staleNaive,
          last_interaction_at: null,
          context_package_id: null,
          attention_level: 'clear',
          selected_refs: [
            { kind: 'task', id: 'task-1', label: 'Task 1' },
          ],
          selected_ref_descriptors: [],
          curated_metrics: [],
          context_sources: [],
        },
      }),
    );

    expect(descriptor).toBeNull();
  });
});

describe('buildMessageStrategicContextDescriptor', () => {
  test('preserves captured message snapshots with stale session timestamps', () => {
    const staleNaive = new Date(Date.now() - 5 * 3600_000).toISOString().replace('Z', '');
    const context = {
      strategic_session: {
        interaction_mode: 'continuous',
        active_surface: 'pipeline',
        session_started_at: staleNaive,
        selected_refs: [
          { kind: 'artifact', id: 'artifact:latest-context', label: 'Latest context report' },
        ],
        selected_ref_descriptors: [],
      },
    } as ProjectMessageContext;

    const descriptor = buildMessageStrategicContextDescriptor(context);

    expect(descriptor).not.toBeNull();
    expect(descriptor?.modeLabel).toBe('Workday');
    expect(descriptor?.surfaceLabel).toBe('Pipeline');
    expect(descriptor?.refCount).toBe(1);
    expect(descriptor?.refs[0]?.label).toBe('Latest context report');
  });

  test('preserves captured message snapshots without freshness timestamps', () => {
    const context = {
      strategic_session: {
        interaction_mode: 'continuous',
        active_surface: 'pipeline',
        context_package_id: 'strategic-context:proj-1',
        selected_refs: [
          { kind: 'artifact', id: 'artifact:latest-context', label: 'Latest context report' },
        ],
        selected_ref_descriptors: [],
      },
    } as ProjectMessageContext;

    const descriptor = buildMessageStrategicContextDescriptor(context);

    expect(descriptor).not.toBeNull();
    expect(descriptor?.modeLabel).toBe('Workday');
    expect(descriptor?.surfaceLabel).toBe('Pipeline');
    expect(descriptor?.contextLabel).toBe('Context captured');
    expect(descriptor?.refCount).toBe(1);
    expect(descriptor?.refs[0]?.label).toBe('Latest context report');
  });

  test('preserves captured selected-ref summaries from message receipts', () => {
    const context = {
      strategic_session: {
        interaction_mode: 'continuous',
        active_surface: 'project',
        context_package_id: 'strategic-context:proj-1',
        selected_refs: [
          { kind: 'task', id: 'task-1', label: 'Old task label' },
        ],
        selected_ref_descriptors: [
          {
            kind: 'task',
            id: 'task-1',
            label: 'Review active wave',
            summary: 'Reviewing context descriptors.',
            status: 'IN_PROGRESS',
          },
        ],
      },
    } as ProjectMessageContext;

    const descriptor = buildMessageStrategicContextDescriptor(context);

    expect(descriptor?.refs[0]).toMatchObject({
      label: 'Review active wave',
      summary: 'Reviewing context descriptors.',
      status: 'IN_PROGRESS',
    });
  });

  test('preserves last_interaction_at from message strategic_session snapshots', () => {
    const recentNaive = new Date(Date.now() - 30 * 60_000).toISOString().replace('Z', '');
    const staleNaive = new Date(Date.now() - 5 * 3600_000).toISOString().replace('Z', '');
    const context = {
      strategic_session: {
        interaction_mode: 'continuous',
        active_surface: 'strategic',
        session_started_at: staleNaive,
        last_interaction_at: recentNaive,
        selected_refs: [],
        selected_ref_descriptors: [],
      },
    } as ProjectMessageContext;

    const descriptor = buildMessageStrategicContextDescriptor(context);

    expect(descriptor).not.toBeNull();
    expect(descriptor?.modeLabel).toBe('Workday');
  });
});
