import { afterEach, describe, expect, test, vi } from 'vitest';
import type { ComponentProps } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicContextRail } from './StrategicContextRail';
import type { StrategicStateResponse } from '../strategicStateApi';

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
      failed: 0,
      completed: 12,
      total: 20,
    },
    cumulative_estimated_cost_usd: 3.5,
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
      ...overrides,
    };
    act(() => {
      root!.render(<StrategicContextRail {...props} />);
    });
    return { host, props };
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
    expect(host.textContent).toContain('linked messages');
  });

  test('drills into existing workbench sections', () => {
    const { host, props } = mount();

    const buttons = Array.from(host.querySelectorAll('button'));
    buttons.find(button => button.textContent?.includes('Model'))?.click();
    buttons.find(button => button.textContent?.includes('Directions'))?.click();
    buttons.find(button => button.textContent?.includes('Pipeline'))?.click();
    buttons.find(button => button.textContent?.includes('Runtime'))?.click();

    expect(props.onOpenProjectSection).toHaveBeenCalledWith('problem-model');
    expect(props.onOpenProjectSection).toHaveBeenCalledWith('directions');
    expect(props.onOpenPipeline).toHaveBeenCalled();
    expect(props.onOpenRuntimeHealth).toHaveBeenCalled();
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
});
