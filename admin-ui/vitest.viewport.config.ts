import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { playwright } from '@vitest/browser-playwright';
import { isBenignPlaywrightRouteRace } from './src/test/viewport-checks.js';

// Browser-mode runner used by `npm run test:viewport`. Kept separate from
// vitest.config.ts so the existing jsdom suite (`npm run test:run`) is
// unaffected by browser-mode setup. Tests opt in via the
// `*.viewport.test.{ts,tsx}` filename suffix.
//
// Tailwind MUST be loaded here too. Without it, every utility class
// (`break-all`, `min-w-0`, `truncate`, `overflow-x-auto`, the entire `w-*`
// family, etc.) is inert at test time. That makes the gate untrustworthy
// in both directions — overflow tests can fail on production-correct
// components (false positive, blocks PRs) and miss real overflow that
// Tailwind would expose (false negative). Mirrors `vite.config.ts`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    include: ['src/**/*.viewport.test.{ts,tsx}'],
    // Browser files share Vitest's Playwright transport route. Serializing
    // files prevents concurrent contexts from fulfilling the same internal
    // request twice ("Route is already handled") under CI load.
    fileParallelism: false,
    // Vitest's Playwright module-mocking transport can race its own route
    // teardown after every viewport assertion has completed, producing an
    // unhandled `route.fulfill: Route is already handled!`. Filter only that
    // exact provider-origin error; application errors and assertion failures
    // remain fatal. viewport-coverage.test.ts asserts the fail-closed boundary.
    onUnhandledError(error) {
      if (isBenignPlaywrightRouteRace(error)) return false;
    },
    css: true,
    setupFiles: ['./src/test/viewport-setup.ts'],
    // JSON reporter writes a structured failure manifest so the regression
    // dispatcher can correlate failed test names → screenshot paths in
    // test-results/ and upload them to the backend for operator review.
    reporters: ['default', 'json'],
    outputFile: { json: './test-results/viewport-failures.json' },
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      instances: [{ browser: 'chromium' }],
      // Capture on failure so CI can upload the PNGs as artifacts —
      // overflow regressions are visual and a stack trace alone often
      // doesn't make the failure mode obvious.
      screenshotFailures: true,
    },
  },
  define: {
    'import.meta.env.VITE_FIREBASE_API_KEY': JSON.stringify('test'),
    'import.meta.env.VITE_FIREBASE_AUTH_DOMAIN': JSON.stringify('test.firebaseapp.com'),
    'import.meta.env.VITE_FIREBASE_PROJECT_ID': JSON.stringify('test'),
    // Set a stable test SHA so VersionMismatchBanner's useEffect can be exercised.
    // Tests stub fetch to return either the same SHA (no mismatch) or a different one.
    'import.meta.env.VITE_EXPECTED_BACKEND_SHA': JSON.stringify('test-expected-sha'),
  },
});
