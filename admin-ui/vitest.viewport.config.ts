import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { playwright } from '@vitest/browser-playwright';

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
    css: true,
    setupFiles: ['./src/test/viewport-setup.ts'],
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
  },
});
