import { beforeEach, describe, expect, test, vi } from 'vitest';
import { apiFetch } from './api';
import { updateStrategicWorkbenchState, type StrategicWorkbenchState } from './strategicStateApi';

vi.mock('./api', () => ({
  apiFetch: vi.fn(),
}));

const apiFetchMock = vi.mocked(apiFetch);

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

const savedWorkbench: StrategicWorkbenchState = {
  version: 1,
  interaction_mode: 'continuous',
  active_surface: 'strategic',
  selected_refs: [],
  session_started_at: null,
  last_interaction_at: null,
};

describe('updateStrategicWorkbenchState', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  test('preserves the arming receipt returned by the backend', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      status: 'ok',
      workbench: savedWorkbench,
      arming: 'steering',
    }));

    await expect(
      updateStrategicWorkbenchState('proj-1', { interaction_mode: 'continuous' }),
    ).resolves.toEqual({
      status: 'ok',
      workbench: savedWorkbench,
      arming: 'steering',
    });

    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/messages/workbench-state', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_id: 'proj-1',
        workbench: { interaction_mode: 'continuous' },
      }),
    });
  });

  test('accepts older workbench responses without an arming receipt', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ workbench: savedWorkbench }));

    await expect(
      updateStrategicWorkbenchState('proj-1', { active_surface: 'pipeline' }),
    ).resolves.toEqual({ workbench: savedWorkbench });
  });
});
