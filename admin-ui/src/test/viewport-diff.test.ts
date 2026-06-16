// @vitest-environment node
// pngjs uses Node built-ins (Buffer, fs) that are absent in happy-dom.
import { describe, it, expect, afterAll } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { PNG } from 'pngjs';

// Import via the scripts path. vitest resolves from the admin-ui root.
const { diffPngs } = await import('../../scripts/viewport-diff.mjs');

// ── helpers ──────────────────────────────────────────────────────────────────

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'viewport-diff-test-'));

afterAll(() => {
  fs.rmSync(TMP, { recursive: true, force: true });
});

/**
 * Create a solid-colour PNG (RGBA) and write it to `filePath`.
 * Returns the file path.
 */
function makePng(
  filePath: string,
  width: number,
  height: number,
  r: number,
  g: number,
  b: number,
): string {
  const png = new PNG({ width, height });
  for (let i = 0; i < width * height; i++) {
    png.data[i * 4 + 0] = r;
    png.data[i * 4 + 1] = g;
    png.data[i * 4 + 2] = b;
    png.data[i * 4 + 3] = 255;
  }
  const buf = PNG.sync.write(png);
  fs.writeFileSync(filePath, buf);
  return filePath;
}

// ── tests ────────────────────────────────────────────────────────────────────

describe('viewport-diff helper — diffPngs', () => {
  it('returns change_pct ≈ 0 for an identical pair and writes a diff PNG', async () => {
    const before = makePng(path.join(TMP, 'same-before.png'), 10, 10, 200, 200, 200);
    const after = makePng(path.join(TMP, 'same-after.png'), 10, 10, 200, 200, 200);
    const diffOut = path.join(TMP, 'same-diff.png');

    const result = await diffPngs(before, after, diffOut);

    expect(result.change_pct).toBe(0);
    expect(result.changed_pixels).toBe(0);
    expect(result.total_pixels).toBe(100);
    expect(fs.existsSync(diffOut)).toBe(true);
  });

  it('computes change percentage and writes a highlighted diff for a changed pair', async () => {
    // Red 10×10 before vs blue 10×10 after — every pixel differs.
    const before = makePng(path.join(TMP, 'diff-before.png'), 10, 10, 255, 0, 0);
    const after = makePng(path.join(TMP, 'diff-after.png'), 10, 10, 0, 0, 255);
    const diffOut = path.join(TMP, 'diff-diff.png');

    const result = await diffPngs(before, after, diffOut);

    expect(result.change_pct).toBeGreaterThan(0);
    expect(result.changed_pixels).toBeGreaterThan(0);
    expect(result.total_pixels).toBe(100);
    // The highlighted diff PNG must be written to disk.
    expect(fs.existsSync(diffOut)).toBe(true);
    const written = fs.statSync(diffOut);
    expect(written.size).toBeGreaterThan(0);
  });

  it('handles dimension mismatch by padding to the union bounding box', async () => {
    // 10×10 red vs 15×8 blue — neither fits cleanly inside the other.
    const before = makePng(path.join(TMP, 'dim-before.png'), 10, 10, 255, 0, 0);
    const after = makePng(path.join(TMP, 'dim-after.png'), 15, 8, 0, 0, 255);
    const diffOut = path.join(TMP, 'dim-diff.png');

    const result = await diffPngs(before, after, diffOut);

    // Union bounding box: 15×10 = 150 pixels.
    expect(result.total_pixels).toBe(150);
    expect(result.changed_pixels).toBeGreaterThan(0);
    expect(fs.existsSync(diffOut)).toBe(true);
  });

  it('rejects when the before PNG path does not exist', async () => {
    const missing = path.join(TMP, 'does-not-exist.png');
    const after = makePng(path.join(TMP, 'reject-after.png'), 4, 4, 0, 0, 0);
    const diffOut = path.join(TMP, 'reject-diff.png');

    await expect(diffPngs(missing, after, diffOut)).rejects.toThrow();
  });
});
