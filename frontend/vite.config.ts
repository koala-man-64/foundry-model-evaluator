import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the Vite server proxies /api to the backend. The backend accepts the Vite
// origin only when it is started with the explicit --dev flag (see backend security guard).
const backend = process.env.FME_BACKEND_URL ?? 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: backend, changeOrigin: false } },
  },
  build: {
    outDir: 'dist',
    target: 'es2022',
    sourcemap: false,
  },
})
