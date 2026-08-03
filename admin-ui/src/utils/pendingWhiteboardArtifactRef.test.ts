import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import {
  ToolArtifactRequestError,
  type ResidentToolArtifactCreateResponse,
} from '../api/projectTools';
import {
  createPendingWhiteboardArtifactOperation,
  loadPendingWhiteboardArtifactOperations,
  persistPendingWhiteboardArtifactOperation,
  settlePendingWhiteboardArtifactOperation,
} from './pendingWhiteboardArtifactRef';

const ARTIFACT_REF = {
  id: 'ref-4',
  artifact_ref_id: 'ref-4',
  tool_id: 'wb-1',
  project_id: 'project-a',
  kind: 'whiteboard' as const,
  revision: 4,
  checksum: 'sha256-image',
  author: 'operator-a',
  timestamp: '2026-07-17T00:00:00Z',
  retrieval: {
    capability: 'project_tool_rendered_image' as const,
    href: '/artifact/ref-4',
  },
};

function operation() {
  return createPendingWhiteboardArtifactOperation({
    actorUserId: 'operator-a',
    projectId: 'project-a',
    toolId: 'wb-1',
    toolTitle: 'Board',
    revision: 4,
    intent: 'ask',
    prompt: 'Review Board revision 4 and answer my question.',
    renderedImage: {
      attachment_id: 'upload-4',
      storage_uri: 'gs://bucket/message-attachments/project-a/upload-4/board.png',
      checksum: 'sha256-image',
      mime_type: 'image/png',
      width: 1600,
      height: 900,
    },
  });
}

describe('pending Whiteboard ArtifactRef operation', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  test('polls 202 to ready with the exact same receipt and rendered payload', async () => {
    const pending = operation();
    expect(persistPendingWhiteboardArtifactOperation(pending)).toBe(true);
    const responses: ResidentToolArtifactCreateResponse[] = [
      {
        state: 'pending',
        promotion_intent_id: 'intent-4',
        receipt_id: 'upload-4',
        retry_after_ms: 5000,
      },
      { state: 'ready', artifact_ref: ARTIFACT_REF },
    ];
    const request = vi.fn<(
      projectId: string,
      toolId: string,
      revision: number,
      renderedImage: typeof pending.rendered_image,
    ) => Promise<ResidentToolArtifactCreateResponse>>(async () => {
      const response = responses.shift();
      if (!response) throw new Error('Expected a queued artifact-ref response.');
      return response;
    });
    const wait = vi.fn(async (delayMs: number) => {
      void delayMs;
    });

    const result = await settlePendingWhiteboardArtifactOperation(pending, { request, wait });

    expect(result.state).toBe('ready');
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[1]).toEqual(request.mock.calls[0]);
    expect(request.mock.calls[0][3]).toBe(pending.rendered_image);
    expect(wait).toHaveBeenCalledTimes(1);
    // The retry deadline is absolute, so the next loop subtracts the small
    // amount of wall time spent persisting the pending operation.
    const [waitMs] = wait.mock.calls[0];
    expect(waitMs).toBeGreaterThanOrEqual(4990);
    expect(waitMs).toBeLessThanOrEqual(5000);
  });

  test('recovers a lost ready response by replaying the same identity', async () => {
    const pending = operation();
    persistPendingWhiteboardArtifactOperation(pending);
    vi.spyOn(Date, 'now').mockReturnValue(pending.updated_at);
    const request = vi.fn()
      .mockRejectedValueOnce(new TypeError('network response lost'))
      .mockResolvedValueOnce({ state: 'ready', artifact_ref: ARTIFACT_REF });
    const wait = vi.fn(async (delayMs: number) => {
      void delayMs;
    });

    const result = await settlePendingWhiteboardArtifactOperation(pending, { request, wait });

    expect(result.state).toBe('ready');
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[1]).toEqual(request.mock.calls[0]);
    expect(wait).toHaveBeenCalledWith(2500);
  });

  test('loads the full operation after remount and resumes exact receipt', async () => {
    const beforeRemount = operation();
    persistPendingWhiteboardArtifactOperation(beforeRemount);

    const [afterRemount] = loadPendingWhiteboardArtifactOperations('operator-a', 'project-a', 'wb-1');
    expect(afterRemount).toMatchObject({
      operation_id: beforeRemount.operation_id,
      intent: 'ask',
      receipt_id: 'upload-4',
      rendered_image: beforeRemount.rendered_image,
    });
    const request = vi.fn().mockResolvedValue({ state: 'ready', artifact_ref: ARTIFACT_REF });
    await settlePendingWhiteboardArtifactOperation(afterRemount, { request });
    expect(request).toHaveBeenCalledWith('project-a', 'wb-1', 4, beforeRemount.rendered_image);
  });

  test('coalesces duplicate settlement attempts for one operation identity', async () => {
    const pending = operation();
    persistPendingWhiteboardArtifactOperation(pending);
    let resolveRequest: ((value: ResidentToolArtifactCreateResponse) => void) | undefined;
    const request = vi.fn(() => new Promise<ResidentToolArtifactCreateResponse>(resolve => {
      resolveRequest = resolve;
    }));

    const first = settlePendingWhiteboardArtifactOperation(pending, { request });
    const second = settlePendingWhiteboardArtifactOperation(pending, { request });
    expect(second).toBe(first);
    expect(request).toHaveBeenCalledTimes(1);
    resolveRequest?.({ state: 'ready', artifact_ref: ARTIFACT_REF });
    await expect(first).resolves.toMatchObject({ state: 'ready' });
  });

  test('persists an exponential next retry so repeated Resume clicks stay below 30/minute', async () => {
    const pending = operation();
    persistPendingWhiteboardArtifactOperation(pending);
    const now = pending.updated_at;
    // Advance one millisecond per sample. The retry deadline and updated_at
    // must still share one scheduling instant rather than racing two clocks.
    vi.spyOn(Date, 'now')
      .mockReturnValueOnce(now)
      .mockReturnValueOnce(now + 1)
      .mockReturnValueOnce(now + 2)
      .mockReturnValueOnce(now + 3);
    const request = vi.fn().mockRejectedValue(new TypeError('network unavailable'));
    const waits: number[] = [];

    const first = await settlePendingWhiteboardArtifactOperation(pending, {
      request,
      maxAttempts: 1,
      wait: async delay => { waits.push(delay); },
    });
    expect(first).toMatchObject({
      state: 'waiting',
      operation: { attempt_count: 1 },
    });
    expect(first.operation.next_retry_at).toBeGreaterThanOrEqual(first.operation.updated_at + 2500);

    await settlePendingWhiteboardArtifactOperation(first.operation, {
      request,
      maxAttempts: 1,
      wait: async delay => { waits.push(delay); },
    });
    expect(waits[0]).toBeGreaterThanOrEqual(2400);
    expect(request).toHaveBeenCalledTimes(2);
  });

  test('terminal rejection preserves user intent and receipt for a fresh render', async () => {
    const pending = operation();
    persistPendingWhiteboardArtifactOperation(pending);

    const result = await settlePendingWhiteboardArtifactOperation(pending, {
      request: vi.fn().mockRejectedValue(
        new ToolArtifactRequestError('revision changed', 409, false),
      ),
    });

    expect(result).toMatchObject({
      state: 'terminal',
      operation: {
        intent: 'ask',
        receipt_id: 'upload-4',
        last_error: 'revision changed',
      },
    });
    expect(loadPendingWhiteboardArtifactOperations('operator-a', 'project-a', 'wb-1'))
      .toEqual([expect.objectContaining({ state: 'terminal', receipt_id: 'upload-4' })]);
  });
});
