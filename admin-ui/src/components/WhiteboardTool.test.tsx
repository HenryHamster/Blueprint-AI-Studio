import { afterEach, beforeAll, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { registerToolKind } from '../toolRegistry';

vi.mock('../firebase', () => ({
  auth: { currentUser: { uid: 'operator-a', email: 'operator@example.com' } },
  getIdToken: vi.fn().mockResolvedValue('token'),
  getIdTokenAfterAuthRestore: vi.fn().mockResolvedValue('token'),
  onAuthChange: vi.fn(),
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
}));

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
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
  ConversationAttachmentUploadError: class ConversationAttachmentUploadError extends Error {},
}));

vi.mock('../utils/renderWhiteboardArtifact', () => ({
  renderWhiteboardArtifact: vi.fn(),
}));

import {
  fetchToolContent,
  saveToolContent,
  ConflictError,
} from '../api/projectTools';
import WhiteboardTool from './WhiteboardTool';

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
