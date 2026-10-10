import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// A separate entry point: no production auth, environment files, or API proxy.
export default defineConfig({
  root: fileURLToPath(new URL('./src/demo', import.meta.url)),
  base: './',
  envDir: false,
  plugins: [react()],
  server: { host: '127.0.0.1', port: 3002, strictPort: true },
  preview: { host: '127.0.0.1', port: 3002, strictPort: true },
  build: { outDir: '../../dist-demo', emptyOutDir: true },
});
