import { afterEach, beforeAll, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { registerToolKind } from '../toolRegistry';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    fetchToolContent: vi.fn(),
    saveToolContent: vi.fn(),
  };
});

import { fetchToolContent, saveToolContent, ConflictError } from '../api/projectTools';
import WhiteboardTool from './WhiteboardTool';

interface Stroke {
  color: string;
  width: number;
  points: { x: number; y: number }[];
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

// Draw one local stroke by dispatching pointer events on the canvas.
function drawStroke(canvas: HTMLCanvasElement) {
  canvas.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 30, clientY: 40 }));
  canvas.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 60, clientY: 70 }));
  canvas.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 60, clientY: 70 }));
}

describe('WhiteboardTool — autosave loop + conflict data-loss', () => {
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
  });

  async function mount() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    if (!host) throw new Error('host not mounted');
    return host.querySelector('canvas') as HTMLCanvasElement;
  }

  test('no autosave fires when strokes are unchanged after a save round-trip', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [S1] }, content_revision: 3 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    const canvas = await mount();

    await act(async () => { drawStroke(canvas); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);

    // The revision refresh must NOT re-fire the debounced save.
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);
  });

  test('loaded-but-never-drawn content never autosaves', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { strokes: [S1] }, content_revision: 3 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    await mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
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

    const canvas = await mount();

    await act(async () => { drawStroke(canvas); });
    // Debounce → save → 409 → merge.
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    // Merged strokes differ from last-saved → autosave persists the merge.
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(saveToolContent).toHaveBeenCalledTimes(2);

    // First save carried the server stroke + the local stroke (2 total).
    const firstStrokes = (vi.mocked(saveToolContent).mock.calls[0][2] as { strokes: Stroke[] }).strokes;
    expect(firstStrokes).toHaveLength(2);

    // The merged re-save preserves the local stroke AND the concurrent server
    // stroke — the losing writer's work is never silently discarded.
    const mergedStrokes = (vi.mocked(saveToolContent).mock.calls[1][2] as { strokes: Stroke[] }).strokes;
    expect(mergedStrokes).toHaveLength(3);
    expect(mergedStrokes).toContainEqual(S1);
    expect(mergedStrokes).toContainEqual(S2);
    // The local stroke (the one that isn't S1 or S2) survived the merge.
    const localOnly = mergedStrokes.filter(
      s => JSON.stringify(s) !== JSON.stringify(S1) && JSON.stringify(s) !== JSON.stringify(S2),
    );
    expect(localOnly).toHaveLength(1);
    // The merged save uses the server-adopted base revision.
    expect(vi.mocked(saveToolContent).mock.calls[1][3]).toBe(9);
  });
});
