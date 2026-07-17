import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    // happy-dom instead of jsdom: jsdom's per-file environment instantiation
    // dominated the unit-suite wall-clock on CI's 2-core runners (~60s of the
    // step was pure `environment` setup). happy-dom is Vitest's first-class
    // alternative and sets up far faster; the suite uses no layout/CSS (that's
    // the separate browser-mode viewport suite), so the no-layout DOM is
    // equivalent for these tests.
    environment: 'happy-dom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'eslint-rules/**/*.test.{js,ts}'],
    exclude: ['node_modules/**', 'src/**/*.viewport.test.{ts,tsx}'],
    // A retry can turn a nondeterministic failure into a green build. Unit
    // tests must pass on their first execution so flakes remain actionable.
    retry: 0,
  },
  define: {
    'import.meta.env.VITE_FIREBASE_API_KEY': JSON.stringify(process.env.VITE_FIREBASE_API_KEY ?? 'ci-placeholder'),
    'import.meta.env.VITE_FIREBASE_AUTH_DOMAIN': JSON.stringify(process.env.VITE_FIREBASE_AUTH_DOMAIN ?? 'ci-placeholder.firebaseapp.com'),
    'import.meta.env.VITE_FIREBASE_PROJECT_ID': JSON.stringify(process.env.VITE_FIREBASE_PROJECT_ID ?? 'ci-placeholder'),
  },
});
