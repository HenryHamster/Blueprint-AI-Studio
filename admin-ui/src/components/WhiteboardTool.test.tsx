import { afterEach, beforeAll, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { registerToolKind } from '../toolRegistry';

vi.mock('../firebase', () => ({
  auth: { currentUser: { uid: 'operator-a', email: 'operator@example.com' } },
  getIdToken: vi.fn().mockResolvedValue('token'),
}));

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    createResidentToolArtifactRef: vi.fn(),
    fetchToolContent: vi.fn(),
    saveToolContent: vi.fn(),
  };
});

vi.mock('../strategicStateApi', async () => {
  const actual = await vi.importActual<typeof import('../strategicStateApi')>('../strategicStateApi');
  return { ...actual, deleteMessageAttachmentUpload: vi.fn() };
});

vi.mock('../utils/conversationAttachmentUpload', () => ({
  uploadConversationAttachments: vi.fn(),
}));

vi.mock('../utils/renderWhiteboardArtifact', () => ({
  renderWhiteboardArtifact: vi.fn(),
}));

vi.mock('../utils/residentToolHandoff', () => ({
  dispatchResidentToolHandoff: vi.fn(),
}));

import {
  createResidentToolArtifactRef,
  fetchToolContent,
  saveToolContent,
  ConflictError,
  ToolArtifactRequestError,
} from '../api/projectTools';
import { deleteMessageAttachmentUpload } from '../strategicStateApi';
import { uploadConversationAttachments } from '../utils/conversationAttachmentUpload';
import { renderWhiteboardArtifact } from '../utils/renderWhiteboardArtifact';
import { dispatchResidentToolHandoff } from '../utils/residentToolHandoff';
import {
  createPendingWhiteboardArtifactOperation,
  persistPendingWhiteboardArtifactOperation,
} from '../utils/pendingWhiteboardArtifactRef';
import WhiteboardTool from './WhiteboardTool';

beforeEach(() => {
  vi.mocked(dispatchResidentToolHandoff).mockReturnValue(true);
});

interface Stroke {
  color: string;
  width: number;
  points: { x: number; y: number }[];
}

interface CanvasPayload {
  version?: number;
  strokes: Stroke[];
  nodes: unknown[];
}

const S1: Stroke = { color: '#e2e8f0', width: 4, points: [{ x: 1, y: 1 }, { x: 2, y: 2 }] };
const S2: Stroke = { color: '#f87171', width: 2, points: [{ x: 9, y: 9 }, { x: 8, y: 8 }] };

const SAMPLE_TOOL = {
  id: 'wb-1',
  kind: 'whiteboard',
  slug: 'whiteboard',
  title: 'Whiteboard',
  enabled: true,
  placement: 'project_section' as const,
  content_revision: 3,
};

const WHITEBOARD_ARTIFACT_REF = {
  id: 'ref-wb-4',
  artifact_ref_id: 'ref-wb-4',
  tool_id: 'wb-1',
  project_id: 'proj-a',
  kind: 'whiteboard' as const,
  revision: 4,
  checksum: 'whiteboard-sha256',
  author: 'operator-a',
  timestamp: '2026-07-17T00:00:00Z',
  retrieval: {
    capability: 'project_tool_rendered_image' as const,
    href: '/api/admin/projects/proj-a/tools/wb-1/artifact-refs/ref-wb-4',
  },
};

const RENDERED_WHITEBOARD = {
  file: new File(['png'], 'whiteboard-r4.png', { type: 'image/png' }),
  checksum: 'whiteboard-sha256',
  width: 1600,
  height: 900,
};

beforeAll(() => {
  registerToolKind('whiteboard', WhiteboardTool);
});

function firePointer(el: Element, type: string, x: number, y: number) {
  el.dispatchEvent(new PointerEvent(type, { bubbles: true, clientX: x, clientY: y, pointerId: 1 }));
}

// Draw one freehand stroke on the surface (component must be in draw mode).
function drawStroke(surface: Element) {
  firePointer(surface, 'pointerdown', 30, 40);
  firePointer(surface, 'pointermove', 60, 70);
  firePointer(surface, 'pointerup', 60, 70);
}

describe('WhiteboardTool — autosave loop + conflict data-loss + save-status', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.resetAllMocks();
    localStorage.clear();
  });

  async function mount() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    if (!host) throw new Error('host not mounted');
    return host;
  }

  function surfaceOf(el: HTMLElement): HTMLElement {
    return el.querySelector('[data-testid="whiteboard-surface"]') as HTMLElement;
  }

  function statusText(el: HTMLElement): string {
    return el.querySelector('[data-testid="whiteboard-save-status"]')?.textContent ?? '';
  }

  // Enter draw mode by clicking the "Draw" radio.
  function enterDrawMode(el: HTMLElement) {
    const drawBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Draw');
    act(() => { drawBtn?.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  }

  test('drawing a stroke triggers a debounced save with the v2 payload', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [] }, content_revision: 3 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    const el = await mount();
    enterDrawMode(el);
    await act(async () => { drawStroke(surfaceOf(el)); });

    // Status reflects the unsaved edit before the debounce fires.
    expect(statusText(el)).toBe('Unsaved changes');

    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(saveToolContent).mock.calls[0][2] as unknown as CanvasPayload;
    expect(payload.version).toBe(2);
    expect(payload.strokes).toHaveLength(1);
    expect(Array.isArray(payload.nodes)).toBe(true);
    expect(vi.mocked(saveToolContent).mock.calls[0][3]).toBe(3); // base revision

    // After a clean round-trip the indicator shows "Saved" (no false positive).
    await act(async () => { await Promise.resolve(); });
    expect(statusText(el)).toBe('Saved');
  });

  test('no autosave fires when content is unchanged after a save round-trip', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [S1] }, content_revision: 3 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    const el = await mount();
    enterDrawMode(el);
    await act(async () => { drawStroke(surfaceOf(el)); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);

    // The revision refresh must NOT re-fire the debounced save.
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);
  });

  test('loaded-but-never-edited content never autosaves and shows Saved', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [S1] }, content_revision: 3 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    const el = await mount();
    expect(statusText(el)).toBe('Saved');
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(saveToolContent).not.toHaveBeenCalled();
  });

  test('legacy {strokes} content loads without a nodes field', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [S1, S2] }, content_revision: 7 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 8 });

    const el = await mount();
    enterDrawMode(el);
    await act(async () => { drawStroke(surfaceOf(el)); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });

    const payload = vi.mocked(saveToolContent).mock.calls[0][2] as unknown as CanvasPayload;
    // Legacy strokes are preserved and upgraded into the v2 payload.
    expect(payload.strokes).toHaveLength(3);
    expect(payload.strokes).toContainEqual(S1);
    expect(payload.strokes).toContainEqual(S2);
  });

  test('unmount flushes a pending edit instead of dropping it (memo 5930a473)', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [] }, content_revision: 3 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    const el = await mount();
    enterDrawMode(el);
    await act(async () => { drawStroke(surfaceOf(el)); });
    // Unmount BEFORE the 1200ms debounce elapses — the pending edit must still
    // be persisted, not silently discarded behind a false "Saved".
    expect(saveToolContent).not.toHaveBeenCalled();
    await act(async () => { root?.unmount(); });
    root = null;
    await act(async () => { await Promise.resolve(); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);
  });

  test('Share with Resident saves before rendering and referencing the exact Whiteboard revision', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [], nodes: [] }, content_revision: 3 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });
    vi.mocked(renderWhiteboardArtifact).mockResolvedValue(RENDERED_WHITEBOARD);
    vi.mocked(uploadConversationAttachments).mockResolvedValue({
      uploaded: [{
        type: 'image',
        attachment_id: 'upload-wb-4',
        storage_uri: 'gs://resident/upload-wb-4',
        name: 'whiteboard-r4.png',
        mime_type: 'image/png',
        size_bytes: 3,
      }],
      visible: [],
    });
    vi.mocked(createResidentToolArtifactRef).mockResolvedValue({ state: 'ready', artifact_ref: WHITEBOARD_ARTIFACT_REF });

    const el = await mount();
    enterDrawMode(el);
    await act(async () => { drawStroke(surfaceOf(el)); });
    const share = Array.from(el.querySelectorAll('button')).find(
      button => button.textContent === 'Share with Resident',
    ) as HTMLButtonElement;
    await act(async () => {
      share.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(saveToolContent).toHaveBeenCalledTimes(1);
    expect(renderWhiteboardArtifact).toHaveBeenCalledWith(
      expect.objectContaining({ strokes: expect.any(Array), nodes: [] }),
      4,
    );
    expect(uploadConversationAttachments).toHaveBeenCalledWith('proj-a', [{
      file: RENDERED_WHITEBOARD.file,
      mime_type: 'image/png',
      name: 'whiteboard-r4.png',
    }]);
    expect(createResidentToolArtifactRef).toHaveBeenCalledWith('proj-a', 'wb-1', 4, {
      attachment_id: 'upload-wb-4',
      storage_uri: 'gs://resident/upload-wb-4',
      checksum: 'whiteboard-sha256',
      mime_type: 'image/png',
      width: 1600,
      height: 900,
    });
    expect(vi.mocked(saveToolContent).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(renderWhiteboardArtifact).mock.invocationCallOrder[0],
    );
    expect(vi.mocked(renderWhiteboardArtifact).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(createResidentToolArtifactRef).mock.invocationCallOrder[0],
    );
    expect(dispatchResidentToolHandoff).toHaveBeenCalledWith(expect.objectContaining({
      artifactRef: WHITEBOARD_ARTIFACT_REF,
      intent: 'share',
    }));
    expect(deleteMessageAttachmentUpload).not.toHaveBeenCalled();
  });

  test('remount resumes a durable receipt without rerendering or reuploading', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [], nodes: [] }, content_revision: 4 });
    vi.mocked(createResidentToolArtifactRef).mockResolvedValue({ state: 'ready', artifact_ref: WHITEBOARD_ARTIFACT_REF });
    const pending = createPendingWhiteboardArtifactOperation({
      actorUserId: 'operator-a',
      projectId: 'proj-a',
      toolId: 'wb-1',
      toolTitle: 'Whiteboard',
      revision: 4,
      intent: 'ask',
      laneOwner: 'Operator owns layout',
      prompt: 'Review Whiteboard revision 4.',
      renderedImage: {
        attachment_id: 'persisted-upload',
        storage_uri: 'gs://resident/persisted-upload',
        checksum: 'whiteboard-sha256',
        mime_type: 'image/png',
        width: 1600,
        height: 900,
      },
    });
    expect(persistPendingWhiteboardArtifactOperation(pending)).toBe(true);

    await mount();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(createResidentToolArtifactRef).toHaveBeenCalledWith(
      'proj-a',
      'wb-1',
      4,
      pending.rendered_image,
    );
    expect(renderWhiteboardArtifact).not.toHaveBeenCalled();
    expect(uploadConversationAttachments).not.toHaveBeenCalled();
    expect(dispatchResidentToolHandoff).toHaveBeenCalledWith(expect.objectContaining({
      intent: 'ask',
      laneOwner: 'Operator owns layout',
      prompt: 'Review Whiteboard revision 4.',
    }));
  });

  test('retains the rendered upload and durable intent if reference creation is uncertain', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [], nodes: [] }, content_revision: 3 });
    vi.mocked(renderWhiteboardArtifact).mockResolvedValue(RENDERED_WHITEBOARD);
    vi.mocked(uploadConversationAttachments).mockResolvedValue({
      uploaded: [{
        type: 'image',
        attachment_id: 'orphan-upload',
        storage_uri: 'gs://resident/orphan-upload',
        name: 'whiteboard-r3.png',
        mime_type: 'image/png',
        size_bytes: 3,
      }],
      visible: [],
    });
    vi.mocked(createResidentToolArtifactRef).mockRejectedValue(
      new ToolArtifactRequestError('revision conflict', 409, false),
    );
    vi.mocked(deleteMessageAttachmentUpload).mockResolvedValue(undefined);

    const el = await mount();
    const share = Array.from(el.querySelectorAll('button')).find(
      button => button.textContent === 'Share with Resident',
    ) as HTMLButtonElement;
    await act(async () => {
      share.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(createResidentToolArtifactRef).toHaveBeenCalled();
    expect(deleteMessageAttachmentUpload).not.toHaveBeenCalled();
    expect(dispatchResidentToolHandoff).not.toHaveBeenCalled();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('revision conflict');
  });

  test('terminal retry preserves intent but creates one fresh render receipt', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [], nodes: [] }, content_revision: 3 });
    vi.mocked(renderWhiteboardArtifact).mockResolvedValue(RENDERED_WHITEBOARD);
    vi.mocked(uploadConversationAttachments)
      .mockResolvedValueOnce({
        uploaded: [{
          type: 'image',
          attachment_id: 'upload-old',
          storage_uri: 'gs://resident/upload-old',
          name: 'whiteboard-r3.png',
          mime_type: 'image/png',
          size_bytes: 3,
        }],
        visible: [],
      })
      .mockResolvedValueOnce({
        uploaded: [{
          type: 'image',
          attachment_id: 'upload-fresh',
          storage_uri: 'gs://resident/upload-fresh',
          name: 'whiteboard-r3.png',
          mime_type: 'image/png',
          size_bytes: 3,
        }],
        visible: [],
      });
    vi.mocked(createResidentToolArtifactRef)
      .mockRejectedValueOnce(new ToolArtifactRequestError('receipt rejected', 400, false))
      .mockResolvedValueOnce({ state: 'ready', artifact_ref: { ...WHITEBOARD_ARTIFACT_REF, revision: 3 } });

    const el = await mount();
    const share = Array.from(el.querySelectorAll('button')).find(
      button => button.textContent === 'Share with Resident',
    ) as HTMLButtonElement;
    await act(async () => {
      share.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const retry = Array.from(el.querySelectorAll('button')).find(
      button => button.textContent === 'Retry with fresh render',
    ) as HTMLButtonElement;
    expect(retry).toBeTruthy();
    await act(async () => {
      retry.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(renderWhiteboardArtifact).toHaveBeenCalledTimes(2);
    expect(uploadConversationAttachments).toHaveBeenCalledTimes(2);
    expect(createResidentToolArtifactRef).toHaveBeenNthCalledWith(
      2,
      'proj-a',
      'wb-1',
      3,
      expect.objectContaining({ attachment_id: 'upload-fresh' }),
    );
    expect(dispatchResidentToolHandoff).toHaveBeenCalledWith(expect.objectContaining({
      intent: 'share',
      artifactRef: expect.objectContaining({ revision: 3 }),
    }));
    expect(deleteMessageAttachmentUpload).not.toHaveBeenCalled();
  });

  test('a failed Whiteboard flush refuses rendering, upload, and Resident reference creation', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [], nodes: [] }, content_revision: 3 });
    vi.mocked(saveToolContent).mockRejectedValue(new Error('storage unavailable'));

    const el = await mount();
    enterDrawMode(el);
    await act(async () => { drawStroke(surfaceOf(el)); });
    const share = Array.from(el.querySelectorAll('button')).find(
      button => button.textContent === 'Share with Resident',
    ) as HTMLButtonElement;
    await act(async () => {
      share.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(saveToolContent).toHaveBeenCalledTimes(1);
    expect(renderWhiteboardArtifact).not.toHaveBeenCalled();
    expect(uploadConversationAttachments).not.toHaveBeenCalled();
    expect(createResidentToolArtifactRef).not.toHaveBeenCalled();
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('not durably saved');
  });

  test('adding a text block persists a text node in the v2 payload', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [], nodes: [] }, content_revision: 1 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 2 });

    const el = await mount();
    const addText = Array.from(el.querySelectorAll('button')).find(b => b.textContent === '+ Text');
    expect(addText).toBeTruthy();
    await act(async () => { addText?.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    // A textarea for the new node should render.
    expect(el.querySelector('textarea')).toBeTruthy();

    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(saveToolContent).mock.calls[0][2] as unknown as CanvasPayload;
    expect(payload.nodes).toHaveLength(1);
    expect((payload.nodes[0] as { type: string }).type).toBe('text');
  });

  test('renders previously-saved image and document nodes from content', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: {
        version: 2,
        strokes: [],
        nodes: [
          { id: 'img-1', type: 'image', x: 10, y: 10, w: 100, h: 80, dataUrl: 'data:image/png;base64,AAAA', name: 'shot.png' },
          { id: 'doc-1', type: 'document', x: 200, y: 10, w: 120, h: 90, dataUrl: 'data:application/pdf;base64,BBBB', name: 'paper.pdf', mime: 'application/pdf' },
        ],
      },
      content_revision: 4,
    });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 5 });

    const el = await mount();
    const img = el.querySelector('img');
    expect(img).toBeTruthy();
    expect(img?.getAttribute('src')).toBe('data:image/png;base64,AAAA');
    // The document node renders a PDF preview referencing the stored data URL.
    expect(el.querySelector('object[type="application/pdf"]')).toBeTruthy();
    // No spurious autosave for content that was only loaded, never edited.
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(saveToolContent).not.toHaveBeenCalled();
  });

  test('a 409 response merges rather than dropping local strokes', async () => {
    vi.mocked(fetchToolContent)
      .mockResolvedValueOnce({ content: { strokes: [S1] }, content_revision: 3 })
      // Another viewer added S2 concurrently.
      .mockResolvedValueOnce({ content: { strokes: [S1, S2] }, content_revision: 9 });
    vi.mocked(saveToolContent)
      .mockRejectedValueOnce(new ConflictError())
      .mockResolvedValueOnce({ content_revision: 10 });

    const el = await mount();
    enterDrawMode(el);
    await act(async () => { drawStroke(surfaceOf(el)); });
    // Debounce → save → 409 → merge.
    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    // Merged strokes differ from last-saved → autosave persists the merge.
    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(saveToolContent).toHaveBeenCalledTimes(2);

    // First save carried the server stroke + the local stroke (2 total).
    const firstStrokes = (vi.mocked(saveToolContent).mock.calls[0][2] as unknown as CanvasPayload).strokes;
    expect(firstStrokes).toHaveLength(2);

    // The merged re-save preserves the local stroke AND the concurrent server
    // stroke — the losing writer's work is never silently discarded.
    const mergedStrokes = (vi.mocked(saveToolContent).mock.calls[1][2] as unknown as CanvasPayload).strokes;
    expect(mergedStrokes).toHaveLength(3);
    expect(mergedStrokes).toContainEqual(S1);
    expect(mergedStrokes).toContainEqual(S2);
    const localOnly = mergedStrokes.filter(
      s => JSON.stringify(s) !== JSON.stringify(S1) && JSON.stringify(s) !== JSON.stringify(S2),
    );
    expect(localOnly).toHaveLength(1);
    // The merged save uses the server-adopted base revision.
    expect(vi.mocked(saveToolContent).mock.calls[1][3]).toBe(9);
  });
});
