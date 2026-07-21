import { describe, it, expect } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useStrategistStatus } from './useStrategistStatus';

describe('useStrategistStatus', () => {
  it('returns idle when nothing is active', () => {
    const { result } = renderHook(() => useStrategistStatus({}));
    expect(result.current.phase).toBe('idle');
    expect(result.current.source).toBe('idle');
  });

  it('returns busy with cycle_progress source when strategistActive', () => {
    const { result } = renderHook(() =>
      useStrategistStatus({
        strategistActive: true,
        cycleProgress: { task_id: 't1', phase_label: 'Planning' },
      }),
    );
    expect(result.current.phase).toBe('busy');
    expect(result.current.source).toBe('cycle_progress');
    expect(result.current.label).toBe('Strategist: Planning…');
  });

  it('returns busy with active_cycle source when activeCycle.has_active_task', () => {
    const { result } = renderHook(() =>
      useStrategistStatus({
        activeCycle: {
          id: 'c1',
          outcome: 'in_progress',
          has_active_task: true,
          phase1_task_id: 't1',
          phase2_task_id: null,
          phase1_started_at: null,
          phase1_completed_at: null,
          phase2_started_at: null,
          phase2_completed_at: null,
          directions_created: 0,
          directions_retired: 0,
          tasks_spawned: 0,
          vision_updated: false,
          problem_model_changed: false,
          running_update_changed: false,
        },
      }),
    );
    expect(result.current.phase).toBe('busy');
    expect(result.current.source).toBe('active_cycle');
    expect(result.current.label).toBe('Strategist working…');
  });

  it('returns busy with pending_steer source when pendingRefreshAt set', () => {
    const { result } = renderHook(() =>
      useStrategistStatus({ pendingRefreshAt: '2026-07-21T10:00:00Z' }),
    );
    expect(result.current.phase).toBe('busy');
    expect(result.current.source).toBe('pending_steer');
    expect(result.current.activityKind).toBe('operator');
  });

  it('returns done with completion source when lastCycleCompletion set', () => {
    const { result } = renderHook(() =>
      useStrategistStatus({ lastCycleCompletion: { status: 'ok', created_count: 2 } }),
    );
    expect(result.current.phase).toBe('done');
    expect(result.current.source).toBe('completion');
    expect(result.current.label).toBe('Cycle complete');
  });

  it('returns error when lastCycleCompletion has error status', () => {
    const { result } = renderHook(() =>
      useStrategistStatus({ lastCycleCompletion: { status: 'error', failure_reason: 'oops' } }),
    );
    expect(result.current.phase).toBe('error');
    expect(result.current.detail).toBe('oops');
  });

  it('returns done with steer_received source when only lastFiredAt set', () => {
    const { result } = renderHook(() =>
      useStrategistStatus({ lastFiredAt: '2026-07-21T10:00:00Z' }),
    );
    expect(result.current.phase).toBe('done');
    expect(result.current.source).toBe('steer_received');
  });

  it('strategistActive takes priority over completion', () => {
    const { result } = renderHook(() =>
      useStrategistStatus({
        strategistActive: true,
        lastCycleCompletion: { status: 'ok' },
      }),
    );
    expect(result.current.phase).toBe('busy');
    expect(result.current.source).toBe('cycle_progress');
  });
});
