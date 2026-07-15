import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { playwright } from '@vitest/browser-playwright';

// Capture-mode variant of vitest.viewport.config.ts, used by
// `npm run test:viewport:capture` and driven by `scripts/viewport-snapshot.sh`.
// It reuses the EXACT same browser harness as the gate (same include glob, same
// `viewport-setup.ts`, same Tailwind + react plugins, same firebase defines) so
// captures render byte-identically to what the assertion gate sees — only three
// deltas distinguish it:
//
//   1. `screenshotFailures: false` — the gate captures PNGs only on failure for
//      CI artifact upload; capture-mode instead writes one PNG per (test, width)
//      from inside the assertion helper (see maybeCapture in src/test/viewport.ts),
//      so failure shots would only pollute the capture output directory.
//   2. Two `define`-injected env vars — `VITE_VIEWPORT_CAPTURE` flips the helper
//      from no-op to capture, and `VITE_VIEWPORT_SHOT_DIR` carries the per-run
//      output directory. The config is evaluated node-side per invocation (same
//      as the firebase keys below), so the shot dir is injectable per run via
//      the `VIEWPORT_SHOT_DIR` environment variable.
//   3. Default reporters — capture is informational, never a gate, so it omits
//      the JSON failure-manifest reporter the gate config wires up.
//
// Tailwind MUST be loaded here too, for the same reason vitest.viewport.config.ts
// documents: without it every utility class is inert and the captured layout
// would not match production.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    include: ['src/**/*.viewport.test.{ts,tsx}'],
    fileParallelism: false,
    css: true,
    setupFiles: ['./src/test/viewport-setup.ts'],
    // Default reporters: capture output is informational (PNGs + manifest are
    // produced by the helper + snapshot script), not a structured gate manifest.
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      instances: [{ browser: 'chromium' }],
      // Keep assertion-failure shots out of the capture directory — capture-mode
      // emits its own one-PNG-per-(test, width) output via maybeCapture().
      screenshotFailures: false,
    },
  },
  define: {
    'import.meta.env.VITE_FIREBASE_API_KEY': JSON.stringify('test'),
    'import.meta.env.VITE_FIREBASE_AUTH_DOMAIN': JSON.stringify('test.firebaseapp.com'),
    'import.meta.env.VITE_FIREBASE_PROJECT_ID': JSON.stringify('test'),
    // Set a stable test SHA so VersionMismatchBanner's useEffect can be exercised.
    'import.meta.env.VITE_EXPECTED_BACKEND_SHA': JSON.stringify('test-expected-sha'),
    // Capture-mode flags, injected node-side per run (mirrors the firebase keys).
    // VITE_VIEWPORT_CAPTURE === '1' flips maybeCapture() from no-op to active;
    // VITE_VIEWPORT_SHOT_DIR is the absolute output root for this invocation.
    'import.meta.env.VITE_VIEWPORT_CAPTURE': JSON.stringify('1'),
    'import.meta.env.VITE_VIEWPORT_SHOT_DIR': JSON.stringify(process.env.VIEWPORT_SHOT_DIR ?? ''),
  },
});
