import { beforeEach, describe, expect, test, vi } from 'vitest';
import { apiFetch } from './api';
import {
  checkInStrategicSession,
  executeStrategicWorkSessionCommand,
  fetchStrategicStateWithRevision,
  fetchStrategicConversation,
  fetchResidentEngineHealth,
  findStrategicMessageByClientIdentity,
  deleteMessageAttachmentUpload,
  getMessageAttachmentUploadUrl,
  getMessageAttachmentDownloadUrl,
  invalidateMessageAttachmentDownloadUrl,
  isStrategicConversationNotModified,
  isStrategicStateNotModified,
  STRATEGIC_STATE_NOT_MODIFIED,
  updateStrategicWorkbenchState,
  type StrategicWorkbenchState,
} from './strategicStateApi';

vi.mock('./api', async () => {
  const { createApiMock } = await import('./test/api-mock-complete');
  return createApiMock();
});

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

describe('fetchStrategicConversation', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  test('sends the held ETag and returns a sentinel for a 304 transcript poll', async () => {
    apiFetchMock.mockResolvedValue(new Response(null, { status: 304 }));

    const result = await fetchStrategicConversation(
      'proj-1',
      { includeMessageId: 'pinned-1', limit: 40 },
      undefined,
      '"conversation-1"',
    );

    expect(isStrategicConversationNotModified(result)).toBe(true);
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/proj-1/strategic-conversation?limit=40&include_message_id=pinned-1',
      { headers: { 'If-None-Match': '"conversation-1"' } },
      2,
      30000,
      undefined,
    );
  });

  test('consumes the direct cursor page contract', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      items: [{ id: 'm-9', sequence: 9 }],
      limit: 25,
      next_before_sequence: 9,
      has_more: true,
    }));

    const result = await fetchStrategicConversation('proj-1', { beforeSequence: 20, limit: 25 });

    if (isStrategicConversationNotModified(result)) throw new Error('unreachable');
    expect(result.conversation).toMatchObject({ next_before_sequence: 9, has_more: true });
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/proj-1/strategic-conversation?before_sequence=20&limit=25',
      undefined,
      2,
      30000,
      undefined,
    );
  });

  test('requests forward pages from the last received sequence', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ items: [], limit: 100, has_more: false }));
    await fetchStrategicConversation('proj-1', { afterSequence: 1_000, limit: 100 });
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/proj-1/strategic-conversation?after_sequence=1000&limit=100',
      undefined,
      2,
      30000,
      undefined,
    );
  });

  test('sends a bounded conversation search independently from pagination', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ items: [], limit: 100, has_more: false }));
    await fetchStrategicConversation('proj-1', { search: 'merge decision', limit: 100 });
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/proj-1/strategic-conversation?limit=100&search=merge+decision',
      undefined,
      2,
      30000,
      undefined,
    );
  });

  test('identity recovery adopts only an exact canonical match', async () => {
    apiFetchMock.mockResolvedValueOnce(jsonResponse({
      items: [{ id: 'fallback', idempotency_key: 'send-1' }],
      matched_message: { id: 'mine', idempotency_key: 'send-1' },
      wakeup_state: 'paused',
      limit: 1,
      has_more: false,
    }));
    await expect(findStrategicMessageByClientIdentity('proj-1', 'send-1')).resolves.toMatchObject({
      message: { id: 'mine' },
      wakeup_state: 'paused',
    });

    apiFetchMock.mockResolvedValueOnce(jsonResponse({
      items: [{ id: 'someone-else', idempotency_key: 'send-2' }], limit: 1, has_more: false,
    }));
    await expect(findStrategicMessageByClientIdentity('proj-1', 'send-1')).resolves.toBeNull();
  });

  test.each(['queued', 'coalesced', 'paused', 'waiting_for_capacity'] as const)(
    'preserves the %s wake receipt during identity recovery',
    async wakeupState => {
      apiFetchMock.mockResolvedValueOnce(jsonResponse({
        items: [],
        matched_message: { id: `message-${wakeupState}`, idempotency_key: 'send-1' },
        wakeup_state: wakeupState,
        limit: 1,
        has_more: false,
      }));
      await expect(findStrategicMessageByClientIdentity('proj-1', 'send-1')).resolves.toMatchObject({
        wakeup_state: wakeupState,
      });
    },
  );
});

describe('fetchResidentEngineHealth', () => {
  beforeEach(() => apiFetchMock.mockReset());

  test('fetches the owner rollout projection without waking the Resident', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ project_id: 'proj-1', resident_initialized: true }));
    await expect(fetchResidentEngineHealth('proj-1')).resolves.toMatchObject({ resident_initialized: true });
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/proj-1/resident-engine/health', undefined, 1, 15000, undefined,
    );
  });

  test.each([403, 404])('quietly omits the owner-only projection on %s', async status => {
    apiFetchMock.mockResolvedValue(new Response(null, { status }));
    await expect(fetchResidentEngineHealth('proj-1')).resolves.toBeNull();
  });
});

describe('message attachment download URL cache', () => {
  beforeEach(() => { apiFetchMock.mockReset(); vi.useFakeTimers(); vi.setSystemTime(new Date('2026-07-15T00:00:00Z')); });
  afterEach(() => { invalidateMessageAttachmentDownloadUrl('proj-cache', 'message-cache', 'attachment-cache'); vi.useRealTimers(); });

  test('deduplicates valid URLs and re-resolves after their bounded expiry', async () => {
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse({ download_url: 'https://signed.example/one', expires_in_seconds: 31 }))
      .mockResolvedValueOnce(jsonResponse({ download_url: 'https://signed.example/two', expires_in_seconds: 31 }));
    const first = await getMessageAttachmentDownloadUrl('proj-cache', 'message-cache', 'attachment-cache');
    const cached = await getMessageAttachmentDownloadUrl('proj-cache', 'message-cache', 'attachment-cache');
    expect(cached).toEqual(first);
    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1_001);
    await expect(getMessageAttachmentDownloadUrl('proj-cache', 'message-cache', 'attachment-cache'))
      .resolves.toMatchObject({ downloadUrl: 'https://signed.example/two' });
    expect(apiFetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('message attachment upload contract', () => {
  beforeEach(() => apiFetchMock.mockReset());

  test('binds the signed upload receipt to the exact byte size', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({
      upload_url: 'https://signed.example/upload',
      download_url: 'https://signed.example/download',
      storage_uri: 'gs://bucket/object',
      attachment_id: 'attachment-1',
      size_bytes: 17,
      expires_in_seconds: 900,
    }));
    await getMessageAttachmentUploadUrl('project-1', 'run.txt', 'text/plain', 17);
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/project-1/messages/attachment-upload-url',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ filename: 'run.txt', content_type: 'text/plain', size_bytes: 17 }),
      }),
    );
  });

  test('deletes an unconsumed upload receipt by project and attachment identity', async () => {
    apiFetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await deleteMessageAttachmentUpload('project-1', 'attachment-1');
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/project-1/messages/attachment-uploads/attachment-1',
      { method: 'DELETE' },
    );
  });
});

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

describe('fetchStrategicStateWithRevision', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  test('returns the parsed state and captures the ETag header into state_etag', async () => {
    apiFetchMock.mockResolvedValue(
      new Response(JSON.stringify({ project_id: 'proj-1', state_revision: 'rev-1' }), {
        status: 200,
        headers: { ETag: '"full-etag-1"' },
      }),
    );

    const result = await fetchStrategicStateWithRevision('proj-1', null);
    expect(result).not.toBe(STRATEGIC_STATE_NOT_MODIFIED);
    if (isStrategicStateNotModified(result)) throw new Error('unreachable');
    expect(result.project_id).toBe('proj-1');
    expect(result.state_etag).toBe('"full-etag-1"');

    // No revision and no etag → no query param, no If-None-Match header.
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/proj-1/strategic-state',
      undefined,
      2,
      30000,
      undefined,
      false,
    );
  });

  test('sends the held revision as ?state_revision and the etag as If-None-Match', async () => {
    apiFetchMock.mockResolvedValue(
      new Response(JSON.stringify({ project_id: 'proj-1' }), { status: 200 }),
    );

    await fetchStrategicStateWithRevision('proj-1', 'rev-9', undefined, '"held-etag"');

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/proj-1/strategic-state?state_revision=rev-9',
      { headers: { 'If-None-Match': '"held-etag"' } },
      2,
      30000,
      undefined,
      false,
    );
  });

  test('maps a 304 response to the not-modified sentinel without parsing the body', async () => {
    // A 304 has an empty body; json() must never be called on it.
    apiFetchMock.mockResolvedValue(new Response(null, { status: 304 }));

    const result = await fetchStrategicStateWithRevision('proj-1', 'rev-1', undefined, '"e1"');
    expect(result).toBe(STRATEGIC_STATE_NOT_MODIFIED);
  });

  test('throws on a non-ok, non-304 response', async () => {
    apiFetchMock.mockResolvedValue(new Response('nope', { status: 500 }));

    await expect(
      fetchStrategicStateWithRevision('proj-1', null),
    ).rejects.toThrow('Failed to fetch strategic state: 500');
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
