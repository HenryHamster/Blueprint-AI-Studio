import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const devProxyTarget = process.env.VITE_DEV_PROXY_TARGET || 'http://127.0.0.1:8080'

// https://vite.dev/config/
export default defineConfig({
  base: '/',
  plugins: [react(), tailwindcss()],
  build: {
    reportCompressedSize: false,
    rollupOptions: {
      maxParallelFileOps: 3,
    },
  },
  server: {
    allowedHosts: true,
    proxy: {
      '/api': {
        target: devProxyTarget,
        changeOrigin: true,
      }
    }
  },
})
