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

  test('writes through the project Strategic session endpoint and preserves the arming receipt', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      project_id: 'proj-1',
      status: 'unavailable',
      location: 'unavailable',
      status_reason: 'no_runner',
      session: {
        version: 1,
        id: 'strategic-session:proj-1',
        mode: 'continuous',
        active_surface: 'strategic',
        selected_refs: [],
        selected_ref_descriptors: [],
        curated_metrics: [],
      },
      workbench: savedWorkbench,
      arming: 'steering',
      conversation: { limit: 20, items: [] },
      recent_outputs: [],
    }));

    await expect(
      updateStrategicWorkbenchState('proj-1', { interaction_mode: 'continuous' }),
    ).resolves.toEqual({
      workbench: savedWorkbench,
      arming: 'steering',
    });

    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/projects/proj-1/strategic-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workbench: { interaction_mode: 'continuous' },
      }),
    });
  });

  test('encodes the project id in the canonical session URL', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ workbench: savedWorkbench }));

    await expect(
      updateStrategicWorkbenchState('owner/repo project', { active_surface: 'pipeline' }),
    ).resolves.toEqual({ workbench: savedWorkbench });

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/owner%2Frepo%20project/strategic-session',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  test('accepts older workbench responses without an arming receipt', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ status: 'ok', workbench: savedWorkbench }));

    await expect(
      updateStrategicWorkbenchState('proj-1', { active_surface: 'pipeline' }),
    ).resolves.toEqual({ status: 'ok', workbench: savedWorkbench });
  });

  test('rejects malformed session responses that omit the saved workbench', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      project_id: 'proj-1',
      status: 'unavailable',
      location: 'unavailable',
      session: {},
      conversation: { limit: 20, items: [] },
      recent_outputs: [],
    }));

    await expect(
      updateStrategicWorkbenchState('proj-1', { active_surface: 'runtime' }),
    ).rejects.toThrow('missing workbench');
  });
});
