import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, cleanup } from '@testing-library/react';
import { useStrategicSession } from './useStrategicSession';
import type { StrategicLivenessResponse, StrategicStateResponse } from '../strategicStateApi';

// Spread the real modules and override only the members under test so the
// mock-factory-export-coverage scanner treats these full-replacement mocks as
// complete (importOriginal delegation). The overridden handlers are never
// invoked here — the tests only read derived state — but keeping the real
// exports avoids partial-mock drift.
vi.mock('../strategicStateApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../strategicStateApi')>()),
  checkInStrategicSession: vi.fn(),
  updateStrategicWorkbenchState: vi.fn(),
}));
vi.mock('../utils/strategicContextDescriptor', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/strategicContextDescriptor')>()),
  buildStrategicContextDescriptor: vi.fn(() => null),
}));

type ReadModel = Parameters<typeof useStrategicSession>[0]['strategicReadModel'];

function makeReadModel(data: StrategicStateResponse | null): ReadModel {
  return {
    data,
    loading: false,
    error: null,
    refresh: vi.fn().mockResolvedValue(undefined),
    applySessionResponse: vi.fn(),
  } as unknown as ReadModel;
}

function makeLiveness(): StrategicLivenessResponse {
  return {
    project_id: 'proj-1',
    interaction_mode: 'continuous',
  };
}

// Minimal heavy read-model state carrying its own (different) interaction mode so
// we can prove the read model's workbench wins once it lands.
function makeHeavyState(): StrategicStateResponse {
  return {
    project_id: 'proj-1',
    strategic: { active_cycle: null, active_task: null, active_phase: null, latest_cycle: null, refresh: { pending: null } },
    workbench: {
      version: 1,
      interaction_mode: 'chat',
      active_surface: 'strategic',
      selected_refs: [],
    },
    running_update: { excerpt: null, char_count: 0, acknowledged_at: null },
    problem_model: { active_gap_count: 0, capability_count: 0, pending_confirmation_count: 0, top_gap: null },
    directions: { total_count: 0, active_count: 0, paused_count: 0, primary: null },
    messages: { counts: { open: 0, linked: 0 }, items: [] },
    tasks: { counts: { queued: 0, running: 0, evaluating: 0, blocked: 0, parked: 0, failed: 0, completed: 0, total: 0 }, cumulative_estimated_cost_usd: 0 },
    design: { has_design_doc: false },
  } as StrategicStateResponse;
}

describe('useStrategicSession liveness fallback', () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => cleanup());

  it('resolves the interaction mode from the liveness probe before the read model lands', () => {
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(null),
        strategicLiveness: makeLiveness(),
      }),
    );
    expect(result.current.strategicInteractionMode).toBe('continuous');
  });

  it('lets the heavy read model workbench take precedence once it hydrates', () => {
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(makeHeavyState()),
        strategicLiveness: makeLiveness(),
      }),
    );
    // Read model workbench wins: the mode comes from it, not the liveness probe.
    expect(result.current.strategicInteractionMode).toBe('chat');
  });

  it('ignores a liveness probe for a different project', () => {
    const other: StrategicLivenessResponse = { ...makeLiveness(), project_id: 'proj-2' };
    const { result } = renderHook(() =>
      useStrategicSession({
        selectedProjectId: 'proj-1',
        addToast: vi.fn(),
        strategicReadModel: makeReadModel(null),
        strategicLiveness: other,
      }),
    );
    expect(result.current.strategicInteractionMode).toBeNull();
  });
});
