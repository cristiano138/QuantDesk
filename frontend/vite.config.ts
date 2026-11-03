import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端只绑 127.0.0.1（宪法）；dev 代理 /api 与 /ws 到 8787
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8787', ws: true },
    },
  },
})
