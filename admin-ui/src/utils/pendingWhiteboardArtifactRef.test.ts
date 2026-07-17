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
    laneOwner: 'Operator keeps layout; Resident verifies assumptions.',
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

  test('polls 202 to 201 with the exact same receipt and rendered payload', async () => {
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
    const request = vi.fn().mockImplementation(async () => responses.shift());

    const result = await settlePendingWhiteboardArtifactOperation(pending, {
      request,
      wait: async () => undefined,
    });

    expect(result.state).toBe('ready');
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[1]).toEqual(request.mock.calls[0]);
    expect(request.mock.calls[0][3]).toBe(pending.rendered_image);
  });

  test('recovers a lost 201 response by replaying the same identity', async () => {
    const pending = operation();
    persistPendingWhiteboardArtifactOperation(pending);
    const request = vi.fn()
      .mockRejectedValueOnce(new TypeError('network response lost'))
      .mockResolvedValueOnce({ state: 'ready', artifact_ref: ARTIFACT_REF });

    const result = await settlePendingWhiteboardArtifactOperation(pending, {
      request,
      wait: async () => undefined,
    });

    expect(result.state).toBe('ready');
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[1]).toEqual(request.mock.calls[0]);
  });

  test('loads the full operation after a remount and resumes without rendering or uploading', async () => {
    const beforeRemount = operation();
    persistPendingWhiteboardArtifactOperation(beforeRemount);

    const [afterRemount] = loadPendingWhiteboardArtifactOperations('operator-a', 'project-a', 'wb-1');
    expect(afterRemount).toMatchObject({
      operation_id: beforeRemount.operation_id,
      intent: 'ask',
      lane_owner: beforeRemount.lane_owner,
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

  test('terminal rejection preserves user intent and receipt for an explicit fresh retry', async () => {
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
        lane_owner: pending.lane_owner,
        receipt_id: 'upload-4',
        last_error: 'revision changed',
      },
    });
    expect(loadPendingWhiteboardArtifactOperations('operator-a', 'project-a', 'wb-1'))
      .toEqual([expect.objectContaining({ state: 'terminal', receipt_id: 'upload-4' })]);
  });
});
