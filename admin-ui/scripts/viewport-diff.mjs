#!/usr/bin/env node
// @ts-check
/**
 * viewport-diff.mjs — perceptual PNG differ for before/after viewport pairs.
 *
 * CLI: node viewport-diff.mjs <beforePng> <afterPng> <diffOutPng>
 * stdout: {"change_pct":<float>,"changed_pixels":<int>,"total_pixels":<int>}
 * exit non-zero on any error (bad input, missing file, decode failure).
 *
 * Also exports `diffPngs(before, after, diffOut)` for unit tests.
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs';
import { dirname } from 'path';
import { PNG } from 'pngjs';
import pixelmatch from 'pixelmatch';

/**
 * Decode a PNG file to an object with {data, width, height}.
 * @param {string} filePath
 */
function decodePng(filePath) {
  const buf = readFileSync(filePath);
  return PNG.sync.read(buf);
}

/**
 * Pad an RGBA image buffer to (targetWidth x targetHeight), filling new pixels
 * with white (255,255,255,255). Returns a new Buffer.
 * @param {Buffer} data original RGBA flat buffer
 * @param {number} srcW
 * @param {number} srcH
 * @param {number} targetW
 * @param {number} targetH
 * @returns {Buffer}
 */
function padToSize(data, srcW, srcH, targetW, targetH) {
  if (srcW === targetW && srcH === targetH) return data;
  const out = Buffer.alloc(targetW * targetH * 4, 255); // fill white
  for (let row = 0; row < srcH; row++) {
    const srcOffset = row * srcW * 4;
    const dstOffset = row * targetW * 4;
    data.copy(out, dstOffset, srcOffset, srcOffset + srcW * 4);
  }
  return out;
}

/**
 * Compute a perceptual diff between two PNG files and write a highlighted diff
 * image to diffOut.
 *
 * @param {string} beforePath absolute or relative path to before PNG
 * @param {string} afterPath absolute or relative path to after PNG
 * @param {string} diffOutPath where to write the highlighted diff PNG
 * @returns {Promise<{change_pct: number, changed_pixels: number, total_pixels: number}>}
 */
export async function diffPngs(beforePath, afterPath, diffOutPath) {
  const before = decodePng(beforePath);
  const after = decodePng(afterPath);

  const w = Math.max(before.width, after.width);
  const h = Math.max(before.height, after.height);

  const beforeData = padToSize(before.data, before.width, before.height, w, h);
  const afterData = padToSize(after.data, after.width, after.height, w, h);

  const diffData = Buffer.alloc(w * h * 4);
  const changedPixels = pixelmatch(beforeData, afterData, diffData, w, h, {
    threshold: 0.1,
    includeAA: false,
  });

  const totalPixels = w * h;
  const changePct = totalPixels === 0 ? 0 : (changedPixels / totalPixels) * 100;

  // Write the diff PNG.
  mkdirSync(dirname(diffOutPath), { recursive: true });
  const diffPng = new PNG({ width: w, height: h });
  diffData.copy(diffPng.data);
  const diffBuf = PNG.sync.write(diffPng);
  writeFileSync(diffOutPath, diffBuf);

  return {
    change_pct: Math.round(changePct * 100) / 100,
    changed_pixels: changedPixels,
    total_pixels: totalPixels,
  };
}

// CLI entry point — only runs when invoked directly, not when imported.
if (process.argv[1] && new URL(import.meta.url).pathname === process.argv[1]) {
  const [, , beforePath, afterPath, diffOutPath] = process.argv;
  if (!beforePath || !afterPath || !diffOutPath) {
    process.stderr.write(
      'usage: node viewport-diff.mjs <beforePng> <afterPng> <diffOutPng>\n',
    );
    process.exit(1);
  }
  diffPngs(beforePath, afterPath, diffOutPath)
    .then((result) => {
      process.stdout.write(JSON.stringify(result) + '\n');
    })
    .catch((err) => {
      process.stderr.write('viewport-diff: ' + String(err.message || err) + '\n');
      process.exit(1);
    });
}
