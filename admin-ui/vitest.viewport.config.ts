import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { playwright } from '@vitest/browser-playwright';
import type { BrowserContext } from 'playwright';
import {
  guardPlaywrightRouteHandler,
  isBenignPlaywrightRouteRace,
} from './src/test/viewport-checks.js';

type RuntimePlaywrightProvider = {
  createContext: (...args: unknown[]) => Promise<BrowserContext>;
};

function stablePlaywrightProvider() {
  const provider = playwright();
  const originalFactory = provider.providerFactory;

  provider.providerFactory = async (...factoryArgs) => {
    const runtimeProvider = await originalFactory(...factoryArgs);
    const runtime = runtimeProvider as unknown as RuntimePlaywrightProvider;
    const originalCreateContext = runtime.createContext.bind(runtime);
    const guardedContexts = new WeakSet<BrowserContext>();

    runtime.createContext = async (...contextArgs) => {
      const context = await originalCreateContext(...contextArgs);
      if (!guardedContexts.has(context)) {
        const originalRoute = context.route.bind(context);
        context.route = ((url, handler, options) =>
          originalRoute(url, guardPlaywrightRouteHandler(handler), options)) as BrowserContext['route'];
        guardedContexts.add(context);
      }
      return context;
    };

    return runtimeProvider;
  };

  return provider;
}

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
    // Keep a process-level backstop for the same exact provider rejection. The
    // provider factory below catches it earlier at the route callback boundary;
    // application errors and assertion failures remain fatal.
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
      provider: stablePlaywrightProvider(),
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
