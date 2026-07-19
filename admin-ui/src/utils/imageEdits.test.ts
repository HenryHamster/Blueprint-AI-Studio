import { describe, test, expect } from 'vitest';
import { outputMimeForEdit, renameForMime, computeCropRegion } from './imageEdits';

describe('outputMimeForEdit', () => {
  test('preserves jpeg (both spellings) and webp', () => {
    expect(outputMimeForEdit('image/jpeg')).toBe('image/jpeg');
    expect(outputMimeForEdit('image/jpg')).toBe('image/jpeg');
    expect(outputMimeForEdit('IMAGE/JPEG')).toBe('image/jpeg');
    expect(outputMimeForEdit('image/webp')).toBe('image/webp');
  });

  test('falls back to png for png and anything canvas cannot re-encode', () => {
    expect(outputMimeForEdit('image/png')).toBe('image/png');
    expect(outputMimeForEdit('image/gif')).toBe('image/png');
    expect(outputMimeForEdit('image/svg+xml')).toBe('image/png');
    expect(outputMimeForEdit('')).toBe('image/png');
    expect(outputMimeForEdit(null)).toBe('image/png');
  });
});

describe('renameForMime', () => {
  test('keeps the name when the extension already matches the mime', () => {
    expect(renameForMime('shot.png', 'image/png')).toBe('shot.png');
    expect(renameForMime('photo.jpg', 'image/jpeg')).toBe('photo.jpg');
    expect(renameForMime('photo.jpeg', 'image/jpeg')).toBe('photo.jpeg');
    expect(renameForMime('pic.webp', 'image/webp')).toBe('pic.webp');
  });

  test('swaps the extension when the format changed', () => {
    expect(renameForMime('anim.gif', 'image/png')).toBe('anim.png');
    expect(renameForMime('photo.png', 'image/jpeg')).toBe('photo.jpg');
    expect(renameForMime('drawing.svg', 'image/png')).toBe('drawing.png');
  });

  test('handles names without an extension', () => {
    expect(renameForMime('screenshot', 'image/png')).toBe('screenshot.png');
    expect(renameForMime('', 'image/jpeg')).toBe('image.jpg');
  });
});

describe('computeCropRegion', () => {
  test('maps a displayed selection onto the natural pixel grid (2x scale)', () => {
    // Image displayed at 400x300 but natural is 800x600 → scale 2x.
    const region = computeCropRegion({ x: 50, y: 25, w: 100, h: 50 }, 400, 300, 800, 600);
    expect(region).toEqual({ sx: 100, sy: 50, sw: 200, sh: 100 });
  });

  test('is identity when display equals natural size', () => {
    const region = computeCropRegion({ x: 10, y: 20, w: 30, h: 40 }, 200, 200, 200, 200);
    expect(region).toEqual({ sx: 10, sy: 20, sw: 30, sh: 40 });
  });

  test('clamps a selection that runs past the image edge', () => {
    const region = computeCropRegion({ x: 150, y: 150, w: 100, h: 100 }, 200, 200, 200, 200);
    // origin clamped to <= natural-1, and width/height clamped to remaining span.
    expect(region.sx).toBe(150);
    expect(region.sy).toBe(150);
    expect(region.sw).toBe(50);
    expect(region.sh).toBe(50);
  });

  test('guarantees at least a 1x1 region for a zero-size selection', () => {
    const region = computeCropRegion({ x: 10, y: 10, w: 0, h: 0 }, 200, 200, 200, 200);
    expect(region.sw).toBeGreaterThanOrEqual(1);
    expect(region.sh).toBeGreaterThanOrEqual(1);
  });

  test('falls back to the full image for degenerate display/natural dimensions', () => {
    expect(computeCropRegion({ x: 0, y: 0, w: 10, h: 10 }, 0, 0, 640, 480))
      .toEqual({ sx: 0, sy: 0, sw: 640, sh: 480 });
  });
});
