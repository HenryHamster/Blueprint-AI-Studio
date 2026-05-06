import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { playwright } from '@vitest/browser-playwright';

// Browser-mode runner used by `npm run test:viewport`. Kept separate from
// vitest.config.ts so the existing jsdom suite (`npm run test:run`) is
// unaffected by browser-mode setup. Tests opt in via the
// `*.viewport.test.{ts,tsx}` filename suffix.
export default defineConfig({
  plugins: [react()],
  test: {
    include: ['src/**/*.viewport.test.{ts,tsx}'],
    css: true,
    setupFiles: ['./src/test/viewport-setup.ts'],
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      instances: [{ browser: 'chromium' }],
      screenshotFailures: false,
    },
  },
  define: {
    'import.meta.env.VITE_FIREBASE_API_KEY': JSON.stringify('test'),
    'import.meta.env.VITE_FIREBASE_AUTH_DOMAIN': JSON.stringify('test.firebaseapp.com'),
    'import.meta.env.VITE_FIREBASE_PROJECT_ID': JSON.stringify('test'),
  },
});
