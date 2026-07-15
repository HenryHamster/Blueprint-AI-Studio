import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// Regression guard for the before/after screenshot pilot (Phase 0). The capture
// config must keep the three deltas vs vitest.viewport.config.ts that the
// snapshot workflow depends on: failure shots OFF, and the two define-injected
// env vars (`VITE_VIEWPORT_CAPTURE`, `VITE_VIEWPORT_SHOT_DIR`) the maybeCapture
// hook reads. Text-read pattern mirrors branch-literal-regression.test.ts.
const ADMIN_UI_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

describe('vitest.viewport.capture.config.ts — capture-mode deltas', () => {
  const src = readFileSync(
    path.join(ADMIN_UI_ROOT, 'vitest.viewport.capture.config.ts'),
    'utf-8',
  );

  it('disables failure screenshots so capture output is one-PNG-per-(test, width)', () => {
    expect(src).toContain('screenshotFailures: false');
  });

  it('define-injects the capture flag and the per-run shot dir', () => {
    expect(src).toContain('import.meta.env.VITE_VIEWPORT_CAPTURE');
    expect(src).toContain("JSON.stringify('1')");
    expect(src).toContain('import.meta.env.VITE_VIEWPORT_SHOT_DIR');
    expect(src).toContain('process.env.VIEWPORT_SHOT_DIR');
  });

  it('reuses the gate harness (same include glob, setup file, Tailwind plugin)', () => {
    expect(src).toContain("include: ['src/**/*.viewport.test.{ts,tsx}']");
    expect(src).toContain("setupFiles: ['./src/test/viewport-setup.ts']");
    expect(src).toContain('tailwindcss()');
  });

  it('serializes viewport files so Playwright routes cannot race each other', () => {
    const gateSrc = readFileSync(
      path.join(ADMIN_UI_ROOT, 'vitest.viewport.config.ts'),
      'utf-8',
    );

    expect(src).toContain('fileParallelism: false');
    expect(gateSrc).toContain('fileParallelism: false');
  });
});

describe('package.json — capture script', () => {
  const pkg = JSON.parse(
    readFileSync(path.join(ADMIN_UI_ROOT, 'package.json'), 'utf-8'),
  ) as { scripts?: Record<string, string> };

  it('exposes test:viewport:capture wired to the capture config', () => {
    expect(pkg.scripts?.['test:viewport:capture']).toBe(
      'vitest run --config vitest.viewport.capture.config.ts',
    );
  });
});
