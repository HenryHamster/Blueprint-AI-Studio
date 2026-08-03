import { beforeEach, describe, expect, test, vi } from 'vitest';

vi.mock('./core', () => ({
  API_BASE_URL: '',
  apiErrorMessage: vi.fn((_payload: unknown, fallback: string) => fallback),
  apiFetch: vi.fn(),
  COST_PER_VM_HR: 0.067,
  MESSAGE_CONTENT_MAX: 20000,
  authGet: vi.fn(),
  authHeaders: vi.fn(),
}));

import { apiFetch } from './core';
import {
  createResidentToolArtifactRef,
  ToolArtifactRequestError,
  ToolArtifactConflictError,
} from './projectTools';

const apiFetchMock = vi.mocked(apiFetch);
const ARTIFACT_REF = {
  id: 'ref-notes-7',
  artifact_ref_id: 'ref-notes-7',
  tool_id: 'notes/id',
  project_id: 'project id',
  kind: 'notes',
  revision: 7,
  checksum: 'sha256-notes',
  author: 'operator-a',
  timestamp: '2026-07-17T00:00:00Z',
  retrieval: {
    capability: 'inline_bounded_text',
    href: '/api/admin/projects/project%20id/tools/notes%2Fid/artifact-refs/ref-notes-7',
  },
};

describe('createResidentToolArtifactRef', () => {
  beforeEach(() => apiFetchMock.mockReset());

  test('posts the exact expected revision and adopts the canonical nested response', async () => {
    apiFetchMock.mockResolvedValue(new Response(JSON.stringify({ state: 'ready', artifact_ref: ARTIFACT_REF }), {
      status: 201,
      headers: { 'Content-Type': 'application/json' },
    }));

    const result = await createResidentToolArtifactRef('project id', 'notes/id', 7);

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/project%20id/tools/notes%2Fid/artifact-refs',
      {
        method: 'POST',
        body: JSON.stringify({ expected_revision: 7 }),
      },
    );
    expect(result).toEqual({ state: 'ready', artifact_ref: ARTIFACT_REF });
  });

  test('sends the durable rendered-image receipt for Whiteboard references', async () => {
    apiFetchMock.mockResolvedValue(new Response(JSON.stringify({
      state: 'ready',
      artifact_ref: { ...ARTIFACT_REF, kind: 'whiteboard', tool_id: 'wb-1' },
    }), { status: 201 }));
    const renderedImage = {
      attachment_id: 'upload-1',
      storage_uri: 'gs://resident/upload-1',
      checksum: 'sha256-image',
      mime_type: 'image/png',
      width: 1600,
      height: 900,
    };

    await createResidentToolArtifactRef('project-a', 'wb-1', 11, renderedImage);

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/admin/projects/project-a/tools/wb-1/artifact-refs',
      {
        method: 'POST',
        body: JSON.stringify({ expected_revision: 11, rendered_image: renderedImage }),
      },
    );
  });

  test('returns a typed pending response for 202', async () => {
    apiFetchMock.mockResolvedValue(new Response(JSON.stringify({
      state: 'pending',
      promotion_intent_id: 'intent-1',
      receipt_id: 'upload-1',
      retry_after_ms: 5000,
    }), { status: 202 }));

    const result = await createResidentToolArtifactRef('project-a', 'wb-1', 11, {
      attachment_id: 'upload-1',
      storage_uri: 'gs://resident/upload-1',
      checksum: 'sha256-image',
    });

    expect(result).toEqual({
      state: 'pending',
      promotion_intent_id: 'intent-1',
      receipt_id: 'upload-1',
      retry_after_ms: 5000,
    });
    expect('artifact_ref' in result).toBe(false);
  });

  test('surfaces a 409 as a revision-aware conflict', async () => {
    apiFetchMock.mockResolvedValue(new Response(JSON.stringify({
      detail: { current_revision: 12, action: 'reload_or_rebase' },
    }), { status: 409 }));

    const error = await createResidentToolArtifactRef('project-a', 'notes-1', 11)
      .then(() => null, cause => cause);

    expect(error).toBeInstanceOf(ToolArtifactConflictError);
    expect(error).toMatchObject({
      currentRevision: 12,
      action: 'reload_or_rebase',
    });
  });

  test('treats 401 as retryable so replay reuses the same receipt', async () => {
    apiFetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: 'Token expired' }), {
      status: 401,
    }));

    const error = await createResidentToolArtifactRef('project-a', 'wb-1', 11, {
      attachment_id: 'upload-1',
      storage_uri: 'gs://resident/upload-1',
      checksum: 'sha256-image',
    }).then(() => null, cause => cause);

    expect(error).toBeInstanceOf(ToolArtifactRequestError);
    expect(error).toMatchObject({ status: 401, retryable: true });
  });
});
