import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/rag/',
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      // 本地协同 sidecar(docker run -p 1234:1234 rag-collab); 置于 /api 之前优先匹配
      '/api/collab': {
        target: 'http://localhost:1234',
        ws: true,
        changeOrigin: true
      },
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true
      }
    }
  }
})