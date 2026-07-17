import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import {
  renderWhiteboardArtifact,
  type WhiteboardArtifactContent,
} from './renderWhiteboardArtifact';

interface FakeImageInstance {
  src: string;
  width: number;
  height: number;
  onload: (() => void) | null;
  onerror: (() => void) | null;
}

function canvasHarness() {
  const drawImage = vi.fn();
  const ctx = {
    fillStyle: '', strokeStyle: '', lineWidth: 1, lineCap: 'butt', lineJoin: 'miter',
    font: '', textBaseline: 'alphabetic',
    fillRect: vi.fn(), save: vi.fn(), restore: vi.fn(), setTransform: vi.fn(),
    beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(),
    roundRect: vi.fn(), fill: vi.fn(), drawImage, fillText: vi.fn(),
    measureText: vi.fn((text: string) => ({ width: text.length * 8 })),
  };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    ctx as unknown as CanvasRenderingContext2D,
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => {
    callback(new Blob(['deterministic-png'], { type: 'image/png' }));
  });
  return { ctx, drawImage };
}

describe('renderWhiteboardArtifact', () => {
  let harness: ReturnType<typeof canvasHarness>;
  beforeEach(() => { harness = canvasHarness(); });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  test('returns a checksummed 1600x900 PNG for the exact revision', async () => {
    const result = await renderWhiteboardArtifact({ strokes: [], nodes: [] }, 17);

    expect(result).toMatchObject({ width: 1600, height: 900 });
    expect(result.file.name).toBe('whiteboard-r17.png');
    expect(result.file.type).toBe('image/png');
    expect(result.checksum).toMatch(/^[a-f0-9]{64}$/);
  });

  test('handles a board larger than the argument-spread limit', async () => {
    const points = Array.from({ length: 150_000 }, (_, index) => ({
      x: index,
      y: index % 900,
    }));
    const content: WhiteboardArtifactContent = {
      strokes: [{ color: '#fff', width: 2, points }],
      nodes: [],
    };

    await expect(renderWhiteboardArtifact(content, 2)).resolves.toMatchObject({
      width: 1600,
      height: 900,
    });
  });

  test('caps image preload concurrency at four but draws in node order', async () => {
    const pending = new Map<string, FakeImageInstance>();
    const started: string[] = [];
    class ControlledImage implements FakeImageInstance {
      width = 100;
      height = 50;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      private value = '';
      get src() { return this.value; }
      set src(value: string) {
        this.value = value;
        started.push(value);
        pending.set(value, this);
      }
    }
    vi.stubGlobal('Image', ControlledImage);
    const { drawImage } = harness;
    const render = renderWhiteboardArtifact({
      strokes: [],
      nodes: ['a', 'b', 'c', 'd', 'e'].map((name, index) => ({
        type: 'image' as const, x: index * 20, y: index * 20,
        w: 100, h: 100, dataUrl: `image-${name}`,
      })),
    }, 3);
    await Promise.resolve();
    expect(started).toEqual(['image-a', 'image-b', 'image-c', 'image-d']);

    pending.get('image-a')?.onload?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(started).toEqual(['image-a', 'image-b', 'image-c', 'image-d', 'image-e']);
    expect(drawImage).not.toHaveBeenCalled();
    for (const source of ['image-b', 'image-c', 'image-d', 'image-e']) pending.get(source)?.onload?.();
    await render;

    expect(drawImage.mock.calls.map((call: unknown[]) => (
      (call[0] as FakeImageInstance).src
    )))
      .toEqual(['image-a', 'image-b', 'image-c', 'image-d', 'image-e']);
  });

  test('fails the handoff if any embedded image cannot be rendered', async () => {
    class FailingImage implements FakeImageInstance {
      width = 100;
      height = 50;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      private value = '';
      get src() { return this.value; }
      set src(value: string) {
        this.value = value;
        queueMicrotask(() => this.onerror?.());
      }
    }
    vi.stubGlobal('Image', FailingImage);

    await expect(renderWhiteboardArtifact({
      strokes: [],
      nodes: [{ type: 'image', x: 0, y: 0, w: 10, h: 10, dataUrl: 'broken' }],
    }, 4)).rejects.toThrow('could not be rendered');
  });

  test('rejects content above the hard point, node, image, and image-byte caps', async () => {
    const tooManyPoints = Array.from({ length: 200_001 }, (_, index) => ({ x: index, y: 0 }));
    await expect(renderWhiteboardArtifact({
      strokes: [{ color: '#fff', width: 1, points: tooManyPoints }], nodes: [],
    }, 5)).rejects.toThrow('200,000 points');

    await expect(renderWhiteboardArtifact({
      strokes: [],
      nodes: Array.from({ length: 2_001 }, () => ({ type: 'text' as const, x: 0, y: 0, w: 1, h: 1 })),
    }, 5)).rejects.toThrow('2,000 nodes');

    await expect(renderWhiteboardArtifact({
      strokes: [],
      nodes: Array.from({ length: 65 }, () => ({ type: 'image' as const, x: 0, y: 0, w: 1, h: 1, dataUrl: 'x' })),
    }, 5)).rejects.toThrow('64 images');

    await expect(renderWhiteboardArtifact({
      strokes: [],
      nodes: [{ type: 'image', x: 0, y: 0, w: 1, h: 1, dataUrl: 'x'.repeat(32 * 1024 * 1024 + 1) }],
    }, 5)).rejects.toThrow('32 MB');

    await expect(renderWhiteboardArtifact({
      strokes: [],
      nodes: [{ type: 'text', x: 0, y: 0, w: 1, h: 1, text: 'x'.repeat(2 * 1024 * 1024 + 1) }],
    }, 5)).rejects.toThrow('node text exceeds the 2 MB');
  });

  test('times out an image load that never settles', async () => {
    vi.useFakeTimers();
    class NeverImage implements FakeImageInstance {
      src = '';
      width = 100;
      height = 50;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
    }
    vi.stubGlobal('Image', NeverImage);
    const render = renderWhiteboardArtifact({
      strokes: [],
      nodes: [{ type: 'image', x: 0, y: 0, w: 1, h: 1, dataUrl: 'never' }],
    }, 6);
    const rejection = expect(render).rejects.toThrow('timed out');
    await vi.advanceTimersByTimeAsync(10_000);
    await rejection;
    vi.useRealTimers();
  });

  test('enforces one global render deadline across successive image batches', async () => {
    vi.useFakeTimers();
    class SlowImage implements FakeImageInstance {
      width = 100;
      height = 50;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      private value = '';
      get src() { return this.value; }
      set src(value: string) {
        this.value = value;
        window.setTimeout(() => this.onload?.(), 9_000);
      }
    }
    vi.stubGlobal('Image', SlowImage);
    const render = renderWhiteboardArtifact({
      strokes: [],
      nodes: Array.from({ length: 8 }, (_, index) => ({
        type: 'image' as const, x: index, y: index, w: 1, h: 1, dataUrl: `slow-${index}`,
      })),
    }, 7);
    const rejection = expect(render).rejects.toThrow('15 second deadline');
    await vi.advanceTimersByTimeAsync(15_000);
    await rejection;
    vi.useRealTimers();
  });

  test('enforces the same deadline when canvas encoding completes late', async () => {
    vi.useFakeTimers();
    vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => {
      window.setTimeout(() => callback(new Blob(['late'], { type: 'image/png' })), 16_000);
    });
    const render = renderWhiteboardArtifact({ strokes: [], nodes: [] }, 8);
    const rejection = expect(render).rejects.toThrow('15 second deadline');
    await vi.advanceTimersByTimeAsync(15_000);
    await rejection;
    await vi.advanceTimersByTimeAsync(1_000);
    vi.useRealTimers();
  });
});
