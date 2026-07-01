import { beforeEach, describe, expect, test, vi } from 'vitest';
import { apiFetch } from './api';
import {
  checkInStrategicSession,
  executeStrategicWorkSessionCommand,
  updateStrategicWorkbenchState,
  type StrategicWorkbenchState,
} from './strategicStateApi';

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

describe('checkInStrategicSession', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  test('records a Strategic check-in through the canonical session endpoint', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      project_id: 'proj-1',
      status: 'idle',
      location: 'locally',
      session: {
        version: 1,
        id: 'strategic-session:proj-1',
        mode: 'chat',
        active_surface: 'strategic',
        selected_refs: [],
        selected_ref_descriptors: [],
        curated_metrics: [],
        last_check_in: {
          message_id: 'msg-check',
          created_at: '2026-06-30T12:00:00Z',
          content: 'Checked in with Strategic.',
          kind: 'manual',
        },
        since_last_check_in: {
          version: 1,
          available: true,
          since: '2026-06-30T12:00:00Z',
          until: '2026-06-30T12:00:01Z',
          messages: { total: 0 },
          tasks: { created: 0, updated: 0 },
          alerts: { raised: 0 },
          strategic: { cycles_completed: 0 },
        },
      },
      workbench: savedWorkbench,
      conversation: { limit: 20, items: [] },
      recent_outputs: [],
      check_in: { status: 'created', message_id: 'msg-check' },
    }));

    await expect(
      checkInStrategicSession('proj-1', {
        kind: 'manual',
        idempotency_key: 'check-in-1',
      }),
    ).resolves.toMatchObject({
      check_in: { status: 'created', message_id: 'msg-check' },
      session: {
        last_check_in: { message_id: 'msg-check' },
      },
    });

    expect(apiFetchMock).toHaveBeenCalledWith('/api/admin/projects/proj-1/strategic-session/check-in', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind: 'manual',
        idempotency_key: 'check-in-1',
      }),
    });
  });

  test('encodes the project id when recording a check-in', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      project_id: 'owner/repo project',
      status: 'idle',
      location: 'locally',
      session: {
        version: 1,
        id: 'strategic-session:owner/repo project',
        mode: 'chat',
        active_surface: 'strategic',
        selected_refs: [],
        selected_ref_descriptors: [],
        curated_metrics: [],
      },
      conversation: { limit: 20, items: [] },
      recent_outputs: [],
    }));

    await checkInStrategicSession('owner/repo project');

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/owner%2Frepo%20project/strategic-session/check-in',
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

describe('executeStrategicWorkSessionCommand', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  test('executes a work-session command through the canonical command endpoint', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      project_id: 'proj-1',
      status: 'idle',
      location: 'locally',
      session: {
        version: 1,
        id: 'strategic-session:proj-1',
        mode: 'monitor',
        active_surface: 'strategic',
        selected_refs: [],
        selected_ref_descriptors: [],
        curated_metrics: [],
      },
      workbench: savedWorkbench,
      conversation: { limit: 20, items: [] },
      recent_outputs: [],
      command: {
        status: 'succeeded',
        applied: true,
        command_id: 'session.mode.monitor',
        receipt: {
          id: 'receipt-1',
          command_id: 'session.mode.monitor',
          label: 'Monitor mode',
          status: 'succeeded',
          summary: 'Command completed: Monitor mode',
        },
      },
    }));

    await expect(
      executeStrategicWorkSessionCommand(
        'owner/repo project',
        'session.mode.monitor',
        { idempotency_key: 'cmd-1', observed_at: 1_782_912_000_000 },
      ),
    ).resolves.toMatchObject({
      command: {
        status: 'succeeded',
        receipt: { id: 'receipt-1', command_id: 'session.mode.monitor' },
      },
    });

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/owner%2Frepo%20project/strategic-session/commands/session.mode.monitor/execute',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idempotency_key: 'cmd-1', observed_at: 1_782_912_000_000 }),
      },
    );
  });

  test('throws backend detail for failed command execution', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ detail: 'Command is disabled' }, 409));

    await expect(
      executeStrategicWorkSessionCommand('proj-1', 'session.mode.chat', { idempotency_key: 'cmd-2' }),
    ).rejects.toThrow('Command is disabled');
  });

  test('rejects malformed success responses without a command receipt', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      project_id: 'proj-1',
      status: 'idle',
      location: 'locally',
      session: {},
      conversation: { limit: 20, items: [] },
      recent_outputs: [],
      command: { status: 'succeeded' },
    }));

    await expect(
      executeStrategicWorkSessionCommand('proj-1', 'session.mode.chat', { idempotency_key: 'cmd-3' }),
    ).rejects.toThrow('missing command receipt');
  });
});
