import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    setupFiles: ['src/testSetup.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json'],
    },
  },
});
